import unittest
from rooibos.data.models import Collection, Record
from rooibos.storage.models import Media, Storage
from caching import cache, cache_set, cache_set_many, cache_get, \
    cache_get_many, get_cached_value


class UniqueSlugTestCase(unittest.TestCase):

    def test_long_unique_slugs(self):
        for i in range(100):
            Collection.objects.create(title='T' * 50)
        for i in range(10):
            Collection.objects.create(title='T' * 100)

    def test_unique_slugs(self):
        g = Collection.objects.create(title='TestUniqueSlugs')
        self.assertEqual('testuniqueslugs', g.name)

        g = Collection.objects.create(title='TestUniqueSlugs')
        self.assertEqual('testuniqueslugs-2', g.name)

        g = Collection.objects.create(title='TestUniqueSlugs')
        self.assertEqual('testuniqueslugs-3', g.name)

    def test_unique_with_something_slugs(self):
        r1 = Record.objects.create()
        r2 = Record.objects.create()

        s = Storage.objects.create(title='Test', system='online')

        m1 = Media.objects.create(record=r1, name='thumb', url='m1', storage=s)
        m2 = Media.objects.create(record=r2, name='thumb', url='m2', storage=s)

        self.assertEqual('thumb', m1.name)
        self.assertEqual('thumb', m2.name)

        m2b = Media.objects.create(
            record=r2, name='thumb', url='m2b', storage=s)

        self.assertEqual('thumb-2', m2b.name)


class CacheTest(unittest.TestCase):

    def setUp(self):
        cache.set('CacheTest', 'Cache is available', timeout=1)
        self.cache_enabled = ('Cache is available' == cache.get('CacheTest'))

    def test_basic_cache(self):
        if not self.cache_enabled:
            return

        cache_set('hello', 'world')
        self.assertEqual('world', cache_get('hello'))

        cache_set('hello', 'there', [Record])
        self.assertEqual('there', cache_get('hello', [Record]))

        cache_set('hello', 'everywhere', [Record, Collection])
        self.assertEqual(
            'everywhere', cache_get('hello', [Record, Collection]))

        self.assertEqual('world', cache_get('hello'))
        self.assertEqual('there', cache_get('hello', [Record]))

        cache_set_many(dict(a=1, b=2, c=3))
        r = cache_get_many(['a', 'b', 'c'])
        self.assertEqual(1, r['a'])
        self.assertEqual(2, r['b'])
        self.assertEqual(3, r['c'])

        cache_set_many(dict(a=9, b=8, c=7), [Record])
        r = cache_get_many(['a', 'b', 'c'], [Record])
        self.assertEqual(9, r['a'])
        self.assertEqual(8, r['b'])
        self.assertEqual(7, r['c'])

    def test_get_cached_value(self):
        if not self.cache_enabled:
            return

        class CountFunctionCalls(object):
            counter = 0

            def get_value(self):
                self.counter += 1
                return 'x'

        cfc = CountFunctionCalls()
        self.assertEqual('x', get_cached_value('cfc', cfc.get_value))
        self.assertEqual('x', get_cached_value('cfc', cfc.get_value))
        self.assertEqual(1, cfc.counter)

        self.assertEqual('x', get_cached_value('cfc', cfc.get_value, [Record]))
        self.assertEqual('x', get_cached_value('cfc', cfc.get_value, [Record]))
        self.assertEqual(2, cfc.counter)
