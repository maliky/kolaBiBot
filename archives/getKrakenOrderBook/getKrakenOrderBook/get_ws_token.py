"""open a (private) connetion to kraken"""
from myconf import api_key, api_sec, api_url
import requests
import urllib.parse
import hashlib
import hmac
import base64
import asyncio

async def get_kraken_signature(urlpath, data, secret):
    """retourne la signature de Kraken"""
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()

    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    sigdigest = base64.b64encode(mac.digest())
    return sigdigest.decode()


# Attaches auth headers and returns results of a POST request
async def kraken_request(uri_path, data, api_key, api_sec):
    """
    Ajoute les entête d'authentification en renvoie
    le resultat d'une requette POST
    """
    headers = {}
    headers["API-Key"] = api_key

    # récupère la signature de kraken comme définie dans 'Authentication' section
    # voir doc Kraken
    headers["API-Sign"] = await get_kraken_signature(uri_path, data, api_sec)
    req = requests.post((api_url + uri_path), headers=headers, data=data)
    return req


async def get_websockets_token():
    """Récupère le token pour l'authentification par websocket"""
    resp = await kraken_request(
        "/0/private/GetWebSocketsToken",
        {"nonce": str(int(1000 * time.time()))},
        api_key,
        api_sec,
    )

    return resp.json()["result"]["token"]
