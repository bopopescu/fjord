import inspect
from functools import total_ordering, wraps

from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.test import TestCase as OriginalTestCase
from django.test.client import Client
from django.test.utils import override_settings

import factory
import waffle
from django_browserid.tests import mock_browserid

# reverse is here for convenience so other test modules import it from
# here rather than importing it from urlresolvers
from fjord.base.urlresolvers import reverse  # noqa
from fjord.base.urlresolvers import is_supported_nonlocale, split_path

from fjord.base.models import Profile


def with_waffle(flagname, flagvalue=True):
    """Decorator that enables a given flag

    You can wrap a test class with this decorator and all the tests in
    the class will run with the waffle flag enabled/disabled.

    You can wrap a single test function with this decorator and that
    test will run with the waffle flag enabled/disabled.

    Usage::

        @with_waffle('some_flag', True)
        class TestClass(TestCase):
            ...

        @with_waffle('some_flag', False)
        def test_my_view():
            ...

    """

    def with_waffle_cls_or_fun(cls_or_func):
        """Class or function decorator for enabling waffle flags"""

        def give_me_waffles(func):
            """Function decorator for enabling the waffle flag"""
            @wraps(func)
            def _give_me_waffles(*args, **kwargs):
                origvalue = None
                # Avoid circular imports
                from waffle.models import Flag
                try:
                    flag = Flag.objects.filter(name=flagname)[0]
                    origvalue = flag.everyone
                    flag.everyone = flagvalue
                except waffle.Flag.DoesNotExist:
                    flag = Flag(name=flagname, everyone=True)
                flag.save()

                try:
                    return func(*args, **kwargs)
                except Exception:
                    # FIXME: This breaks if saving the flag also
                    # raises an exception, but that really shouldn't
                    # happen in our test suite and if it does, we've
                    # probably got other more serious issues to deal
                    # with.
                    if origvalue is not None:
                        flag.everyone = origvalue
                        flag.save()
                    raise
            return _give_me_waffles

        if inspect.isclass(cls_or_func):
            # If cls_or_func is a class, then we wrap all the callable
            # methods that start with 'test'.
            for attr in cls_or_func.__dict__.keys():
                if (attr.startswith('test')
                        and callable(getattr(cls_or_func, attr))):

                    setattr(cls_or_func, attr,
                            give_me_waffles(getattr(cls_or_func, attr)))
            return cls_or_func
        else:
            # If cls_or_func is a function, then we return the
            # skipping_fun
            return give_me_waffles(cls_or_func)

    return with_waffle_cls_or_fun


@total_ordering
class EqualAnything(object):
    def __eq__(self, other):
        return True

    def __lt__(self, other):
        return True


WHATEVER = EqualAnything()


class LocalizingClient(Client):
    """Client which rewrites urls to include locales and adds a user agent.

    This prevents the locale middleware from returning a 301 to add the
    prefixes, which makes tests more complicated.

    This pays attention to ``settings.SUPPORTED_NONLOCALES``.

    It also ensures there is a user agent set in the header. The default is a
    Firefox 14 on Linux user agent. It can be overridden by passing a
    user_agent parameter to ``__init__``, setting ``self.user_agent`` to the
    desired value, or by including ``HTTP_USER_AGENT`` in an individual
    request. This behavior can be prevented by setting ``self.user_agent`` to
    ``None``.

    """
    def __init__(self, user_agent=None, *args, **kwargs):
        self.user_agent = (
            user_agent
            or ('Mozilla/5.0 (X11; Linux x86_64; rv:14.0) '
                'Gecko/20100101 Firefox/14.0.1')
        )
        super(LocalizingClient, self).__init__(*args, **kwargs)

    def request(self, **request):
        """Make a request, ensuring it has a locale and a user agent."""
        # Fall back to defaults as in the superclass's implementation:
        path = request.get('PATH_INFO', self.defaults.get('PATH_INFO', '/'))
        locale, shortened = split_path(path)
        if not locale and not is_supported_nonlocale(path):
            request['PATH_INFO'] = '/%s/%s' % (settings.LANGUAGE_CODE,
                                               shortened)
        if 'HTTP_USER_AGENT' not in request and self.user_agent:
            request['HTTP_USER_AGENT'] = self.user_agent

        return super(LocalizingClient, self).request(**request)


def template_used(response, template_name):
    """Asserts a given template was used (with caveats)

    First off, this is a gross simplification of what the Django
    assertTemplateUsed() TestCase method does. This does not work as a
    context manager and it doesn't handle a lot of the pseudo-response
    cases.

    However, it does work with Jinja2 templates provided that
    monkeypatch_render() has patched ``django.shortcuts.render`` to
    add the information required.

    Also, it's not tied to TestCase.

    Also, it uses fewer characters to invoke. For example::

        self.assertTemplateUsed(resp, 'new_user.html')

        assert template_used(resp, 'new_user.html')

    :arg response: HttpResponse object
    :arg template_name: the template in question

    :returns: whether the template was used

    """
    templates = []
    # templates is an array of TemplateObjects
    templates += [t.name for t in getattr(response, 'templates', [])]
    # jinja_templates is a list of strings
    templates += getattr(response, 'jinja_templates', [])
    return template_name in templates


class BaseTestCase(OriginalTestCase):
    def client_login_user(self, user):
        with mock_browserid(user.email):
            ret = self.client.login(audience='faux', assertion='faux')
            assert ret, 'Login failed.'

    def setUp(self):
        super(BaseTestCase, self).setUp()
        cache.clear()

    def tearDown(self):
        super(BaseTestCase, self).tearDown()
        cache.clear()


@override_settings(ES_LIVE_INDEX=False)
class TestCase(BaseTestCase):
    """TestCase that skips live indexing."""
    pass


class MobileTestCase(TestCase):
    """Base class for mobile tests"""
    def setUp(self):
        super(MobileTestCase, self).setUp()
        self.client.cookies[settings.MOBILE_COOKIE] = 'on'


class ProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Profile

    # We pass in profile=None to prevent UserFactory from creating
    # another profile (this disables the RelatedFactory)
    user = factory.SubFactory('fjord.base.tests.UserFactory', profile=None)


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    username = factory.Sequence(lambda n: 'user_%d' % n)
    email = factory.Sequence(lambda n: 'joe%d@example.com' % n)

    # We pass in 'user' to link the generated Profile to our
    # just-generated User This will call
    # ProfileFactory(user=our_new_user), thus skipping the SubFactory.
    profile = factory.RelatedFactory(ProfileFactory, 'user')


class AnalyzerProfileFactory(ProfileFactory):
    @factory.post_generation
    def groups(self, create, extracted, **kwargs):
        if not create:
            return

        self.user.groups.add(Group.objects.get(name='analyzers'))
