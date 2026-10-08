from html.parser import HTMLParser


import json


import os


import re


import time


from datetime import datetime, timezone


class ChannelTextParser(HTMLParser):
    def __init__(self, username):
        super().__init__(convert_charrefs=True)
        self.username = username.casefold()
        self.post = None
        self.depth = 0
        self.parts = []
        self.links = []
        self.messages = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-post' in attrs:
            self.post = attrs['data-post']
        if tag == 'div' and 'tgme_widget_message_text' in attrs.get('class', '').split():
            self.depth = 1
            self.parts, self.links = [], []
        elif self.depth:
            if tag == 'div':
                self.depth += 1
            elif tag == 'br':
                self.parts.append('\n')
            elif tag == 'a':
                self.links.append((len(self.parts), attrs.get('href', '')))

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag == 'a' and self.links:
            start, url = self.links.pop()
            label = ''.join(self.parts[start:])
            # Autolinks/mentions are plain text in Bot API; only text links use Markdown.
            if url.startswith(('https://', 'http://')) and label != url and not label.startswith(('@', '#')):
                self.parts[start:] = [f'[{label}]({url})']
        elif tag == 'div':
            self.depth -= 1
            if not self.depth and self.post:
                channel, _, message_id = self.post.partition('/')
                if channel.casefold() == self.username and message_id.isdigit():
                    self.messages[message_id] = ''.join(self.parts).strip()


