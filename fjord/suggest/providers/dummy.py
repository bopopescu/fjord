import logging

from fjord.suggest import Link, Provider

PROVIDER = 'dummy'
PROVIDER_VERSION = 1


logger = logging.getLogger('i.dummyprovider')


class DummyProvider(Provider):
    def load(self):
        logger.debug('dummy load')

    def get_suggestions(self, response):
        logger.debug('dummy get_suggestions')
        return [
            Link(
                provider=PROVIDER,
                provider_version=PROVIDER_VERSION,
                summary=u'summary {0}'.format(response.description),
                description=u'description {0}'.format(response.description),
                url=response.url
            )
        ]