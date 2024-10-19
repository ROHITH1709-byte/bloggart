import logging
import re
from io import StringIO  # Use io.StringIO for Python 3 compatibility

from django.utils import html
from django.utils import text

import config
import utils

# Import markup modules
import markdown
import markdown_processor  # Ensure this is a valid import
import textile
from docutils.core import publish_parts


CUT_SEPARATOR_REGEX = r'<!--.*cut.*-->'


def render_rst(content):
    """Render ReStructuredText to HTML."""
    warning_stream = StringIO()
    parts = publish_parts(
        content,
        writer_name='html4css1',
        settings_overrides={
            '_disable_config': True,
            'embed_stylesheet': False,
            'warning_stream': warning_stream,
            'report_level': 2,
        }
    )
    rst_warnings = warning_stream.getvalue()
    if rst_warnings:
        logging.warning(rst_warnings)  # Use logging.warning instead of warn
    return parts['html_body']


def render_markdown(content):
    """Render Markdown to HTML with a code block preprocessor."""
    md = markdown.Markdown()
    md.textPreprocessors.insert(0, markdown_processor.CodeBlockPreprocessor())
    return md.convert(content)


def render_textile(content):
    """Render Textile to HTML."""
    return textile.textile(content.encode('utf-8'))


# Mapping: string ID -> (human readable name, renderer)
MARKUP_MAP = {
    'html':     ('HTML', lambda c: c),
    'txt':      ('Plain Text', lambda c: html.linebreaks(html.escape(c))),
    'markdown': ('Markdown', render_markdown),
    'textile':  ('Textile', render_textile),
    'rst':      ('ReStructuredText', render_rst),
}


def get_renderer(post):
    """Returns a render function for this post's body markup."""
    return MARKUP_MAP.get(post.body_markup, (None, lambda c: c))[1]


def clean_content(content):
    """Clean up the raw body by removing the cut separator."""
    return re.sub(CUT_SEPARATOR_REGEX, '', content)


def render_body(post):
    """Return the post's body rendered to HTML."""
    renderer = get_renderer(post)
    return renderer(clean_content(post.body))


def render_summary(post):
    """Return the post's summary rendered to HTML."""
    renderer = get_renderer(post)
    match = re.search(CUT_SEPARATOR_REGEX, post.body)
    if match:
        return renderer(post.body[:match.start(0)])
    else:
        return text.truncate_html_words(renderer(clean_content(post.body)), config.summary_length)
