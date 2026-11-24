# Last modified: 2026-11-05--1138
#
# ku_kte_port.py — test helper (not shipped): Kobo Touch Extended's KoboSpan insertion, ported
# verbatim from its `container.py` (add_kobo_spans / _add_kobo_spans_to_node /
# _append_kobo_spans_from_text, jgoguen/calibre-kobo-driver master, GPL v3) by the step 4 reviewer
# (review-probes-s4/p_kte_numbering.py), so the tests can make a KEPUB numbered the way Kobo Touch
# Extended numbers it. Needs lxml (calibre-debug).
#
# Copyright (C) 2026 CalibReader
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file for details.

import re
import zipfile
from collections import defaultdict
from copy import deepcopy

from lxml import etree

from kobo_position import Book

XHTML_NAMESPACE = 'http://www.w3.org/1999/xhtml'
SKIPPED_TAGS = frozenset(['button', 'circle', 'defs', 'figcaption', 'figure', 'g', 'input', 'math', 'path',
                          'polygon', 'pre', 'rect', 'script', 'style', 'svg', 'use', 'video'])
SPECIAL_TAGS = frozenset(['img'])
TEXT_SPLIT_RE = re.compile(r'(.*?(?:[\.\!\?\:][\'"“”‘’…]*(?=\s)|(?=\s*$)))', re.UNICODE | re.MULTILINE)


class KTE(object):
    def __init__(self):
        self.paragraph_counter = defaultdict(lambda: 1)

    def _add_kobo_spans_to_node(self, node, name):
        if node is None or isinstance(node, (etree._Comment, etree._ProcessingInstruction)):
            if node is not None:
                node.tail = None
            return node
        special_tag_match = re.search(r'^(?:\{[^\}]+\})?(\w+)$', node.tag)
        if special_tag_match:
            if special_tag_match.group(1) in SKIPPED_TAGS:
                return node
            if special_tag_match.group(1) in SPECIAL_TAGS:
                span = etree.Element('{%s}span' % XHTML_NAMESPACE,
                                     attrib={'id': 'kobo.{}.1'.format(self.paragraph_counter[name]), 'class': 'koboSpan'})
                span.append(node)
                return span
        node_text = node.text
        node_children = deepcopy(node.getchildren())
        node_attrs = {k: node.get(k) for k in list(node.keys())}
        node.clear()
        for k, v in node_attrs.items():
            node.set(k, v)
        if node_text is not None:
            if self._append_kobo_spans_from_text(node, node_text, name):
                self.paragraph_counter[name] += 1
        for child in node_children:
            child_tail = child.tail
            child.tail = None
            node.append(self._add_kobo_spans_to_node(child, name))
            if child_tail is not None:
                if self._append_kobo_spans_from_text(node, child_tail, name):
                    self.paragraph_counter[name] += 1
        return node

    def _append_kobo_spans_from_text(self, node, text, name):
        if not text or text == '':
            return False
        groups = TEXT_SPLIT_RE.split(text)
        if len(node) == 0:
            node.text = groups[0]
        else:
            node[-1].tail = groups[0]
        segment_counter = 1
        for g, ws in zip(groups[1::2], groups[2::2]):
            if g.strip() == '':
                continue
            span = etree.Element('{%s}span' % XHTML_NAMESPACE, attrib={
                'class': 'koboSpan', 'id': 'kobo.{}.{}'.format(self.paragraph_counter[name], segment_counter)})
            span.text = g
            span.tail = ws
            node.append(span)
            segment_counter += 1
        return len(groups) > 1


def kte_kepub(epub_path, out_path):
    '''A copy of the EPUB with Kobo Touch Extended's KoboSpans.'''
    src = Book(epub_path)
    members = set(m.lower() for m in src.chapters)
    src.close()
    kte = KTE()
    with zipfile.ZipFile(epub_path) as zin, zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.lower() in members:
                root = etree.fromstring(data, parser=etree.XMLParser(recover=True, resolve_entities=False))
                body = root.find('{%s}body' % XHTML_NAMESPACE)
                if body is not None:
                    kte._add_kobo_spans_to_node(body, info.filename)
                    data = etree.tostring(root, encoding='utf-8', xml_declaration=True)
            zout.writestr(info, data)
    return out_path
