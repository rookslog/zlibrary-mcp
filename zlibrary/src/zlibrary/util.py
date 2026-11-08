import aiohttp
import asyncio

from aiohttp_socks import ChainProxyConnector

from .exception import LoopError
from .logger import logger
from .eapi import raise_for_refused_redirect
from aiohttp.abc import AbstractCookieJar
from typing import Tuple


HEAD = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36"
}


TIMEOUT = aiohttp.ClientTimeout(total=30, connect=0, sock_connect=10, sock_read=30)

HEAD_TIMEOUT = aiohttp.ClientTimeout(total=4, connect=0, sock_connect=4, sock_read=4)


async def GET_request(url, cookies=None, proxy_list=None) -> str:
    try:
        async with aiohttp.ClientSession(
            headers=HEAD,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            cookies=cookies,
            timeout=TIMEOUT,
            connector=ChainProxyConnector.from_urls(proxy_list) if proxy_list else None,
        ) as sess:
            logger.info(f"GET {url}")
            logger.debug(f"Request cookies for {url}: {cookies}")
            # Legacy requests can carry session cookies or keys in the URL.
            async with sess.get(url, allow_redirects=False) as resp:
                raise_for_refused_redirect(resp)
                response_text = await resp.text()
                logger.debug(f"Response status for {url}: {resp.status}")
                logger.debug(f"Response headers for {url}: {resp.headers}")
                if response_text:
                    logger.debug(
                        f"Response body for {url} (first 1000 chars): {response_text[:1000]}"
                    )
                    if len(response_text) > 1000:
                        logger.debug(
                            f"Response body for {url} is longer than 1000 chars, full length: {len(response_text)}"
                        )
                    else:
                        logger.debug(f"Full response body for {url}: {response_text}")
                else:
                    logger.debug(f"Response body for {url} is EMPTY.")
                return response_text
    except asyncio.exceptions.CancelledError:
        raise LoopError("Asyncio loop has been closed before request could finish.")


async def GET_request_cookies(
    url, cookies=None, proxy_list=None
) -> Tuple[str, AbstractCookieJar]:
    try:
        async with aiohttp.ClientSession(
            headers=HEAD,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            cookies=cookies,
            timeout=TIMEOUT,
            connector=ChainProxyConnector.from_urls(proxy_list) if proxy_list else None,
        ) as sess:
            logger.info("GET %s" % url)
            async with sess.get(url, allow_redirects=False) as resp:
                raise_for_refused_redirect(resp)
                return (await resp.text(), sess.cookie_jar)
    except asyncio.exceptions.CancelledError:
        raise LoopError("Asyncio loop has been closed before request could finish.")


async def POST_request(url, data, proxy_list=None):
    try:
        async with aiohttp.ClientSession(
            headers=HEAD,
            timeout=TIMEOUT,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            connector=ChainProxyConnector.from_urls(proxy_list) if proxy_list else None,
        ) as sess:
            logger.info("POST %s" % url)
            # Login forms contain a password; 307/308 would replay the body.
            async with sess.post(url, data=data, allow_redirects=False) as resp:
                raise_for_refused_redirect(resp)
                return (await resp.text(), sess.cookie_jar)
    except asyncio.exceptions.CancelledError:
        raise LoopError("Asyncio loop has been closed before request could finish.")


async def HEAD_request(url, proxy_list=None):
    try:
        async with aiohttp.ClientSession(
            headers=HEAD,
            timeout=HEAD_TIMEOUT,
            connector=ChainProxyConnector.from_urls(proxy_list) if proxy_list else None,
        ) as sess:
            logger.info("Checking connectivity of %s..." % url)
            async with sess.head(url) as resp:
                return resp.status
    except asyncio.exceptions.CancelledError:
        raise LoopError("Asyncio loop has been closed before request could finish.")
    except asyncio.exceptions.TimeoutError:
        return 0


# --- EAPI helpers ---


async def eapi_login(domain: str, email: str, password: str):
    """Convenience: login via EAPI, return (remix_userid, remix_userkey, client).

    Creates an EAPIClient, calls login, returns the authenticated client
    along with credentials for persistence.
    """
    from .eapi import EAPIClient

    client = EAPIClient(domain)
    result = await client.login(email, password)
    if result.get("success") != 1:
        raise Exception(f"EAPI login failed: {result}")
    return (client.remix_userid, client.remix_userkey, client)


async def discover_eapi_domain(eapi_client):
    """Call get_domains() and return the first *usable* advertised EAPI domain.

    Advertised domains are probed before being returned (ISSUE-API-002:
    /eapi/info/domains advertises DiamWall-walled domains first, so returning
    the raw primary entry would steer a working client onto a dead domain).
    Returns None when nothing advertised passes the probe — callers should
    keep the domain they already have.
    """
    from .eapi import select_advertised_domain

    result = await eapi_client.get_domains()
    domains = result.get("domains", [])
    return await select_advertised_domain(domains, eapi_client.domain)
