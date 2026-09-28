from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from pricehunter.domain.errors import ProviderUnavailableError


def parse_xml(payload: bytes) -> Element:
    try:
        root: Element = fromstring(
            payload, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        if sum(1 for _ in root.iter()) > 20000:
            raise ProviderUnavailableError()
        return root
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise ProviderUnavailableError() from exc
