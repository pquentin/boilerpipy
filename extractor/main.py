# -*- coding: utf-8 -*-
import re

import logging
logging.basicConfig()
from collections import defaultdict
import HTMLParser

from lxml.etree import tostring, tounicode, ParserError, iterwalk
from lxml.html.clean import Cleaner
import lxml.html as html
from lxml.etree import tostring

from expressions import *
from common import *
from error import *

logger = logging.getLogger('extractor')

class Extractor:

    def __init__(self, input, notify=None, **options):
        self.input = input.replace('\r','')
        self.options = defaultdict(lambda: None)
        for k, v in options.items():
            self.options[k] = v
        self.notify = notify or logger.info
        self.html = None
        self.TEXT_LENGTH_THRESHOLD = 25
        self.RETRY_LENGTH = 250
        logger.setLevel(level=self.options['loglevel'])

    def _html(self, force=False):
        if force or self.html is None:
            cleaner = Cleaner(scripts=True, javascript=True, comments=True,
                              style=True, links=True, meta=False, add_nofollow=False,
                              page_structure=False, processing_instructions=True, embedded=False,
                              frames=False, forms=False, annoying_tags=False, remove_tags=None,
                              remove_unknown_tags=False, safe_attrs_only=False)
            if isinstance(self.input, unicode):
                # Work around: ValueError: Unicode strings with encoding
                # declaration are not supported by lxml
                self.input = self.input.encode('utf-8')
            self.html = parse(cleaner.clean_html(self.input), self.options['url'], notify=self.notify)
        return self.html

    def content(self):
        return get_body(self._html())

    def title(self):
        return get_title(self._html())

    def summary(self):
        try:
            ruthless = True
            while True:
                self._html(True)
                [i.drop_tree() for i in self.tags(self.html, 'script', 'style', 'noscript')]

                if ruthless: self.remove_unlikely_candidates()
                self.transform_misused_divs_into_paragraphs()
                candidates = self.score_paragraphs(self.options.get('min_text_length', self.TEXT_LENGTH_THRESHOLD))

                best_candidate = self.select_best_candidate(candidates)
                if best_candidate:
                    article = self.get_article(candidates, best_candidate)
                else:
                    if ruthless:
                        ruthless = False
                        self.debug("Ended up stripping too much - going for a safer parsing scheme")
                        # try again
                        continue
                    else:
                        logger.debug("Ruthless and simple parsing did not work. Returning unprocessed raw html")
                        if self.html.find('body') is not None:
                            article = self.html.find('body')
                        else:
                            article = self.html

                content_scores = []
                best_score = 0.0
                new_candidate = None

                for x in candidates:
                    if candidates[x]['content_score'] < 0:
                        continue
                    content_scores.append(candidates[x]['content_score'])

                try:
                    best_score = sorted(content_scores)[len(content_scores)-1:][0]
                except Exception as e:
                    pass

                cleaned_article = self.sanitize(article, candidates)
                of_acceptable_length = len(cleaned_article or '') >= (self.options['retry_length'] or self.RETRY_LENGTH)
                if ruthless and not of_acceptable_length:
                    ruthless = False
                    continue # try again
                else:
                    return cleaned_article
        except (StandardError, ParserError), e:
            logger.exception('error getting summary: ' )
            return None

        except lxml.etree.XMLSyntaxError:
            logger.exception('error getting summary: ' )
            return None

    def get_article(self, candidates, best_candidate):
        # Now that we have the top candidate, look through its siblings for content that might also be related.
        # Things like preambles, content split by ads that we removed, etc.

        sibling_score_threshold = max([10, best_candidate['content_score'] * 0.2])
        output = parse("<div/>")
        for sibling in best_candidate['elem'].getparent().getchildren():
            append = False
            if sibling is best_candidate['elem']:
                append = True
            sibling_key = sibling
            if sibling_key in candidates and candidates[sibling_key]['content_score'] >= sibling_score_threshold:
                append = True

            if sibling.tag == "p":
                link_density = self.get_link_density(sibling)
                node_content = sibling.text or ""
                node_length = len(node_content)

                if node_length > 80 and link_density < 0.25:
                    append = True
                elif node_length < 80 and link_density == 0 and re.search('\.( |$)', node_content):
                    append = True

            if append:
                output.append(sibling)
        if output is not None: output.append(best_candidate['elem'])
        return output

    def select_best_candidate(self, candidates):
        sorted_candidates = sorted(candidates.values(), key=lambda x: x['content_score'], reverse=True)
        self.debug("Top 5 candidates:")
        for candidate in sorted_candidates[:5]:
            elem = candidate['elem']
            self.debug("Candidate %s with score %s '%s...'" % (describe(elem), candidate['content_score'], snippet(elem)))

        if len(sorted_candidates) == 0:
            return None
        best_candidate = sorted_candidates[0]
        self.debug("Best candidate %s with score %s" % (describe(best_candidate['elem']), best_candidate['content_score']))
        return best_candidate

    def get_link_density(self, elem):
        link_length = len("".join([i.text_content() or "" for i in elem.findall(".//a")]))
        text_length = len(elem.text_content())
        return float(link_length) / max(text_length, 1)

    def score_paragraphs(self, min_text_length):
        candidates = {}
        self.debug(str([describe(node) for node in self.tags(self.html, "div")]))
        elems = self.tags(self.html, "div", "p", "td", 'li', "a")

        for elem in elems:
            parent_node = elem.getparent()
            grand_parent_node = parent_node.getparent()
            elem_key = elem
            parent_key = parent_node
            grand_parent_key = grand_parent_node

            inner_text = elem.text_content()

            # If this paragraph is less than 25 characters, don't even count it.
            if (not inner_text) or len(inner_text) < min_text_length:
                continue

            if parent_key not in candidates:
                candidates[parent_key] = self.score_node(parent_node)
            if grand_parent_node is not None and grand_parent_key not in candidates:
                candidates[grand_parent_key] = self.score_node(grand_parent_node)

            content_score = 1
            content_score += len(inner_text.split(','))
            content_score += min([(len(inner_text) / 100), 3])
            if elem not in candidates:
                candidates[elem_key] = self.score_node(elem)
            candidates[elem_key]['content_score'] += content_score
            candidates[parent_key]['content_score'] += content_score
            if grand_parent_node is not None:
                candidates[grand_parent_key]['content_score'] += content_score / 2.0

        # Scale the final candidates score based on link density. Good content should have a
        # relatively small link density (5% or less) and be mostly unaffected by this operation.
        for elem, candidate in candidates.items():
            link_density = self.get_link_density(elem)
            candidate['content_score'] *= (1 - link_density)
            if candidate['content_score'] > 0:
                self.debug("candidate %s scored %s (linkd: %s) '%s'" % (describe(elem), candidate['content_score'], link_density, snippet(elem,30)))

        return candidates

    def class_weight(self, e):
        weight = 0
        if e.get('class', None):
            if REGEXPS.get('negative').search(e.get('class')):
                weight -= 25

            if REGEXPS.get('positive').search(e.get('class')):
                weight += 25

        if e.get('id', None):
            if REGEXPS.get('negative').search(e.get('id')):
                weight -= 25

            if REGEXPS.get('positive').search(e.get('id')):
                weight += 25

        return weight

    def score_node(self, elem):
        content_score = self.class_weight(elem)
        tag = elem.tag.lower()
        if tag == "div":
            content_score += 5
        elif tag == "blockquote":
            content_score += 3
        elif tag == "form":
            content_score -= 3
        elif tag == "th":
            content_score -= 5
        return { 'content_score': content_score, 'elem': elem }

    def debug(self, *a):
            logger.debug(*a)

    def remove_unlikely_candidates(self):
        remove_list = []
        context = iterwalk(self.html)
        for action, elem in context:
            s = "%s%s" % (elem.get('class', ''), elem.get('id', ''))
            if REGEXPS['unlikelyCandidates'].search(s) and (not REGEXPS['okMaybeItsACandidate'].search(s)) and elem.tag != 'body':
                self.debug("Removing unlikely candidate - %s" % (s,))
                remove_list.append(elem)
        [e.drop_tree() for e in remove_list if e.tag != 'html']

    def transform_misused_divs_into_paragraphs(self):
        for elem in self.html.iter():
            if elem.tag.lower() == "div":
                # transform <div>s that do not contain other block elements into <p>s
                if not REGEXPS['divToPElements'].search(unicode(''.join(map(tostring, list(elem))))):
                    self.debug("Altering div(#%s.%s) to p" % (elem.get('id', ''), elem.get('class', '')))
                    elem.tag = "p"

    def tags(self, node, *tag_names):
        for tag_name in tag_names:
            for e in node.findall('.//%s' %tag_name):
                yield e

    def sanitize(self, node, candidates):
        for header in self.tags(node, "h1", "h2", "h3", "h4", "h5", "h6"):
            if self.class_weight(header) < 0 or self.get_link_density(header) > 0.33: header.drop_tree()

        for elem in self.tags(node, "form"):
            elem.drop_tree()
        allowed = {}
        # Conditionally clean <table>s, <ul>s, and <div>s
        for el in self.tags(node, "table", "ul", "div"):
            if el in allowed:
                continue
            weight = self.class_weight(el)
            el_key = el
            if el_key in candidates:
                content_score = candidates[el_key]['content_score']
            else:
                content_score = 0

            tag = el.tag
            if weight + content_score < 0:
                el.drop_tree()
                self.debug("Conditionally cleaned %s with weight %s and content score %s because score + content score was less than zero." %
                    (describe(el), weight, content_score))
            elif len(el.text_content().split(",")) < 10:
                counts = {}
                for kind in ['p', 'img', 'li', 'a', 'embed', 'input', 'iframe']:
                    counts[kind] = len(el.findall('.//%s' % kind))
                counts["li"] -= 100

                content_length = len(el.text_content()) # Count the text length excluding any surrounding whitespace
                link_density = self.get_link_density(el)
                parent_node = el.getparent()
                if parent_node is not None:
                    if parent_node in candidates:
                        content_score = candidates[parent_node]['content_score']
                    else:
                        content_score = 0
                    pweight = self.class_weight(parent_node) + content_score
                    pname = parent_node.tag
                else:
                    pweight = 0
                    pname = "no parent"
                to_remove = False
                reason = ""

                if counts["p"] and counts["img"] > counts["p"]:
                    reason = "too many images"
                    to_remove = True
                elif counts["li"] > counts["p"] and tag != "ul" and tag != "ol":
                    reason = "more <li>s than <p>s"
                    to_remove = True
                elif counts["input"] > (counts["p"] / 3):
                    reason = "less than 3x <p>s than <input>s"
                    to_remove = True
                elif content_length < (self.options.get('min_text_length', self.TEXT_LENGTH_THRESHOLD)) and (counts["img"] == 0):
                    reason = "too short a content length without a single image"
                    to_remove = True
                elif weight < 25 and link_density > 0.2:
                    reason = "too many links for its weight less than 25 (#{weight})"
                    to_remove = True
                elif weight >= 25 and link_density > 0.5:
                    reason = "too many links for its weight (#{weight})"
                    to_remove = True
                elif el.tag.lower() == "embed":
                    if not REGEXPS.get('videos').search(el.get('src')):
                        to_remove = True
                elif el.tag.lower() == "iframe":
                    if not REGEXPS.get('videos').search(el.get('src')):
                        to_remove = True
                elif (counts["embed"] == 1 and content_length < 75) or counts["embed"] > 2:
                    reason = "<embed>s with too short a content length, or too many <embed>s"
                    to_remove = True
                elif (counts["iframe"] == 1 and content_length < 75) or counts["iframe"] > 2:
                    reason = "<iframe>s with too short a content length, or too many <iframe>s"
                    to_remove = True
                if to_remove:
                    self.debug("Conditionally cleaned %s#%s.%s with weight %s and content score %s because it has %s." %
                               (el.tag, el.get('id',''), el.get('class', ''), weight, content_score, reason))
                    self.debug("pname %s pweight %s" %(pname, pweight))
                    el.drop_tree()
        return tounicode(node)

