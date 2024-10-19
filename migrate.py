import datetime
import itertools
import logging
import re
import urllib.parse
from xml.etree import ElementTree
from django.utils import simplejson
from django.utils import html
from google.appengine.api import urlfetch
from google.appengine.ext import db
from google.appengine.ext import deferred

import config
import models
import post_deploy

import pygments
import pygments.lexers
import pygments.formatters
import pygments.util


def disqus_request(method, request_type=urlfetch.GET, **kwargs):
    """Make a request to the Disqus API."""
    kwargs['api_version'] = '1.1'
    if request_type == urlfetch.GET:
        url = f"http://disqus.com/api/{method}?{urllib.parse.urlencode(kwargs)}"
        payload = None
    else:
        url = f"http://disqus.com/api/{method}/"
        payload = urllib.parse.urlencode(kwargs)
    
    response = urlfetch.fetch(url, payload, method=request_type)
    if response.status_code != 200:
        raise Exception("Invalid status code", response.status_code, response.content)

    result = simplejson.loads(response.content)
    if not result['succeeded']:
        raise Exception("RPC did not succeed", result)
    
    return result


class BaseMigration:
    def __init__(self, disqus_user_key, disqus_forum_name):
        forums = disqus_request('get_forum_list', user_api_key=disqus_user_key)
        for forum in forums['message']:
            if forum['shortname'] == disqus_forum_name:
                self.forum_key = disqus_request(
                    'get_forum_api_key',
                    user_api_key=disqus_user_key,
                    forum_id=forum['id']
                )['message']
                return
        raise Exception("Forum not found", disqus_forum_name)


class BloogBreakingMigration(BaseMigration):
    class Article(db.Model):
        title = db.StringProperty()
        article_type = db.StringProperty()
        html = db.TextProperty()
        published = db.DateTimeProperty()
        updated = db.DateTimeProperty()
        tags = db.StringListProperty()

    class Comment(db.Model):
        name = db.StringProperty()
        email = db.StringProperty()
        homepage = db.StringProperty()
        body = db.TextProperty()
        published = db.DateTimeProperty()

    def migrate_one_comment(self, thread_id, comment_key, replies, parent_id=None):
        """Migrate a single comment."""
        comment = self.Comment.get(comment_key)
        post_args = {
            'request_type': urlfetch.POST,
            'thread_id': thread_id,
            'message': html.strip_tags(comment.body).encode('utf-8'),
            'author_name': comment.name.encode('utf-8') if comment.name else 'Someone',
            'author_email': comment.email.encode('utf-8') if comment.email else 'nobody@notdot.net',
            'forum_api_key': self.forum_key,
            'created_at': comment.published.strftime('%Y-%m-%dT%H:%M'),
        }
        if comment.homepage:
            post_args['author_url'] = comment.homepage.encode('utf-8')
        if parent_id:
            post_args['parent_post'] = parent_id
        
        post_id = disqus_request('create_post', **post_args)['message']['id']
        
        for parent_id, replies in itertools.groupby(replies, lambda x: x[0]):
            parent_key = db.Key.from_path('Comment', parent_id, parent=comment_key)
            deferred.defer(self.migrate_one_comment, thread_id, parent_key, [x[1:] for x in replies if x[1:]], post_id)

    def migrate_all_comments(self, article_key, title):
        """Migrate all comments associated with an article."""
        thread_id = disqus_request(
            'thread_by_identifier',
            request_type=urlfetch.POST,
            identifier=str(article_key),
            forum_api_key=self.forum_key,
            title=title
        )['message']['thread']['id']
        
        disqus_request(
            'update_thread',
            request_type=urlfetch.POST,
            forum_api_key=self.forum_key,
            thread_id=thread_id,
            url=f"http://{config.host}{article_key.name()}"
        )

        q = self.Comment.all(keys_only=True).ancestor(article_key)
        comment_ids = sorted(tuple(x.to_path()[3::2]) for x in q.fetch(1000))
        
        for parent_id, replies in itertools.groupby(comment_ids, lambda x: x[0]):
            parent_key = db.Key.from_path('Comment', parent_id, parent=article_key)
            deferred.defer(self.migrate_one_comment, thread_id, parent_key, [x[1:] for x in replies if x[1:]])

    def migrate_one(self, article):
        """Migrate a single article."""
        post = models.BlogPost(
            path=article.key().name(),
            title=article.title,
            body=article.html,
            tags=set(article.tags),
            published=article.published,
            updated=article.updated,
            deps={},
        )
        post.put()
        deferred.defer(self.migrate_all_comments, article.key(), article.title)

    def migrate_all(self, batch_size=20, start_key=None):
        """Migrate all articles in batches."""
        q = self.Article.all()
        if start_key:
            q.filter('__key__ >', start_key)
        
        articles = q.fetch(batch_size)
        for article in articles:
            self.migrate_one(article)
        
        if len(articles) == batch_size:
            deferred.defer(self.migrate_all, batch_size, articles[-1].key())
        else:
            logging.warning("Migration finished; starting rebuild.")
            regen = post_deploy.PostRegenerator()
            deferred.defer(regen.regenerate)


class WordpressMigration(BaseMigration):
    ns_wordpress = 'http://wordpress.org/export/1.0/'
    ns_rss = 'http://purl.org/rss/1.0/modules/content/'

    def __init__(self, export_file, disqus_user_key, disqus_forum_name):
        super().__init__(disqus_user_key, disqus_forum_name)
        self._export_file = export_file

    @classmethod
    def _parse_date(cls, date_str):
        """Parse a date string into a datetime object."""
        return datetime.datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')

    @classmethod
    def _get_text(cls, node, tag, ns=None):
        """Get text content from a node."""
        if ns is not None:
            tag = f'{{{ns}}}{tag}'
        item = node.find(tag)
        return item.text if item is not None else ''

    @classmethod
    def _expand_wp_tags(cls, content):
        """Expand WordPress-specific tags in the content."""
        content = cls._expand_caption_tag(content)
        return cls._expand_sourcecode_tag(content)

    @classmethod
    def _expand_caption_tag(cls, content):
        """Replace caption tags with divs."""
        content = re.sub(r'\[caption[^\]]*\]', '<div class="image">', content)
        return content.replace('[/caption]', '</div>')

    @classmethod
    def _expand_sourcecode_tag(cls, content):
        """Replace source code tags with highlighted code."""
        p_bgn = re.compile(r'\[sourcecode( language="(?P<lang>[a-z]+)")?\]', re.IGNORECASE | re.MULTILINE | re.UNICODE | re.DOTALL)
        p_end = re.compile(r'\[/sourcecode\]', re.IGNORECASE | re.MULTILINE | re.UNICODE | re.DOTALL)
        
        while (match := p_bgn.search(content)):
            bgnidx = match.start()
            m_end = p_end.search(content[match.end():])
            if m_end is None:
                return content
            
            new_content = [content[:match.start()]]
            scode = content[match.end():m_end.start() + match.end()]
            lang = match.groupdict().get('lang')

            if lang is not None:
                try:
                    lexer = pygments.lexers.get_lexer_by_name(lang)
                    formatter = pygments.formatters.get_formatter_by_name('html')
                    scode = pygments.highlight(scode, lexer, formatter)
                except pygments.util.ClassNotFound:
                    logging.info('No lexer found: %s', lang)
                    new_content.extend(['<pre>', scode, '</pre>'])
            else:
                new_content.extend(['<pre>', scode, '</pre>'])

            new_content.append(content[m_end.end() + match.end():])
            content = ''.join(new_content)

        return content

    def migrate_one(self, wp_post):
        """Migrate a single WordPress post."""
        post = models.BlogPost(
            path=wp_post['path'],
            title=wp_post['title'],
            body=wp_post['body'],
            body_markup='html',
            tags=wp_post['tags'],
            published=wp_post['published'],
            updated=wp_post['published'],
            deps={},
        )
        post.put()
        if wp_post['comments']:
            deferred.defer(self.migrate_all_comments, wp_post['comments'], post.path, wp_post['title'])

    def migrate_all_comments(self, wp_comments, post_path, title):
        """Migrate comments for a WordPress post."""
        thread_id = disqus_request(
            'thread_by_identifier',
            request_type=urlfetch.POST,
            identifier=post_path,
            forum_api_key=self.forum_key,
            title=title
        )['message']['thread']['id']
        
        disqus_request(
            'update_thread',
            request_type=urlfetch.POST,
            forum_api_key=self.forum_key,
            thread_id=thread_id,
            url=f"http://{config.host}{post_path}"
        )
        
        for comment in wp_comments[0]:
            deferred.defer(self.migrate_one_comment, comment, thread_id, wp_comments)

    def migrate_one_comment(self, comment, thread_id, comments, parent_id=None):
        """Migrate a single comment."""
        post_args = {
            'request_type': urlfetch.POST,
            'thread_id': thread_id,
            'message': html.strip_tags(comment['message']),
            'author_name': comment['author_name'],
            'author_email': comment['author_email'],
            'forum_api_key': self.forum_key,
            'created_at': comment['date'].strftime('%Y-%m-%dT%H:%M'),
        }
        if comment['author_url']:
            post_args['author_url'] = comment['author_url']
        if parent_id:
            post_args['parent_post'] = parent_id

        post_id = disqus_request('create_post', **post_args)['message']['id']
        
        for reply in comments.get(comment['id'], []):
            logging.info('Adding reply')
            deferred.defer(self.migrate_one_comment, reply, thread_id, comments, post_id)

    def _convert_post_node(self, node, channel_link):
        """Convert an XML node to a post dictionary."""
        post = {
            'title': self._get_text(node, 'title'),
            'body': self._expand_wp_tags(self._get_text(node, 'encoded', ns=self.ns_rss).replace(u'\xa0', ' ')),
            'status': self._get_text(node, 'status', ns=self.ns_wordpress),
            'published': None,
            'tags': set(),
        }

        if post['status'] == 'draft':
            post['published'] = datetime.datetime.max
            post['path'] = None
        else:
            post['published'] = self._parse_date(self._get_text(node, 'post_date', ns=self.ns_wordpress))
            post['path'] = self._get_text(node, 'link')[len(channel_link):] or None

        post['tags'] = {x.get('nicename').decode('utf-8') for x in node.findall('category') if x.get('nicename') is not None}
        post['comments'] = self._get_comment_map(node)

        return post

    def _get_comment_map(self, node):
        """Create a mapping of comments by parent ID."""
        cmap = {}  # mapping: parent comment id --> list of comments
        for comment in node.findall(f'{{self.ns_wordpress}}comment'):
            if self._get_text(comment, 'comment_approved', self.ns_wordpress) != '1':
                continue
            
            cmt = {
                'message': self._get_text(comment, 'comment_content', ns=self.ns_wordpress).encode('utf-8') or None,
                'author_name': (self._get_text(comment, 'comment_author', ns=self.ns_wordpress) or 'Someone').encode('utf-8'),
                'author_email': self._get_text(comment, 'comment_author_email', ns=self.ns_wordpress).encode('utf-8') or f'someone@{config.host}',
                'author_url': self._get_text(comment, 'comment_author_url', ns=self.ns_wordpress).encode('utf-8') or None,
                'date': self._parse_date(self._get_text(comment, 'comment_date', ns=self.ns_wordpress)),
                'id': int(self._get_text(comment, 'comment_id', ns=self.ns_wordpress)),
                'parent': int(self._get_text(comment, 'comment_parent', ns=self.ns_wordpress)),
            }
            cmap.setdefault(cmt['parent'], []).append(cmt)

        return cmap

    def _get_posts(self):
        """Retrieve and parse posts from the export file."""
        dom = ElementTree.parse(self._export_file)
        channel = dom.find('channel')
        channel_link = self._get_text(channel, 'link')
        
        return [
            self._convert_post_node(x, channel_link)
            for x in channel.findall('item')
            if self._get_text(x, 'post_type', ns=self.ns_wordpress) == 'post'
        ]

    def migrate_all(self, batch_size=20, items=None):
        """Migrate all posts in batches."""
        if items is None:
            items = self._get_posts()
        
        logging.warning('Start processing of %d items', len(items))
        
        for item in items[:batch_size]:
            self.migrate_one(item)
        
        if items[batch_size:]:
            deferred.defer(self.migrate_all, batch_size, items[batch_size:])
        else:
            logging.warning("Migration finished; starting rebuild.")
            regen = post_deploy.PostRegenerator()
            deferred.defer(regen.regenerate)
