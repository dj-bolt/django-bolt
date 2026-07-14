"""App under test for RS256 JWT auth against a real runbolt server.

Regression coverage for https://github.com/dj-bolt/django-bolt/issues/261:
JWTAuthentication configured with an RSA public key and algorithms=["RS256"]
always rejected valid tokens, because the Rust auth code built the decoding
key with DecodingKey::from_secret (HMAC-only) regardless of algorithm.

The keypair below is static (not generated at import time): this module gets
imported independently by both the test process (to sign tokens) and the
subprocess server (to configure the route), so a freshly-generated keypair
would differ between the two and fail for a reason unrelated to the bug
under test - a fixed keypair keeps them consistent.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCU6CRyLhqdNeVm
3Xd4UrHCjG4D6qlf5d/Nik4UvxDm/VA43++6Tw1vfgZO++IRQCaQXF3Z4F76HR6t
yhjCRgyBOQQPSqOKz6KWySFe//PbPFiLPB3QuVEOPyj0aRmdneQNNrJlHADqiH2A
M/lBlUBmvfwXu3rCJd05dZMeuSshSZdvLl9lk/+9TKxaEP7/C/gye84mO26rcjOw
VzPcYUNdrQ1Yrb0OshT3ki3i+scxvQyhyCPKl5jHJJlLvYwL0HerQdpMv2Yu38fg
EAFQA9lZPWUyZQy/GkYG6dMxj87hiEzLGdISkagRsBGTzXW1XPitINQtumGVPbun
yTCfRCXJAgMBAAECggEACuEsizHL0gFhFx3xSledvfLYCu3h8tbEflZaPsZ4Vq2F
Z1Jn8pCEu8j/xFTqVIuqWvvUrkQ9durFFg+LtY4RVdOznzu7LTuJy+3lbp/RfQrb
W/jyzhBBgtf3MbxdbxhdKYtTMh3/VyN31UkhhLrXRL/MI8kX6V2Dh1zheJTbjjKf
bCvlPyJNo9Kwf/nunnSjpEPtC9VzODY08fLPZofXJejUd3qumNfa+9uObSYftyMG
EK9oegrNPABlnYENIPYGcBj4goG/jimRUbh+LLLtCfTiacyBooUSNy4Roqg68UnM
GShFEkyA6xaR+U/C9NM1JEEwWFuXXsrGJZWRNorlbQKBgQDGPx6dB5I0kckUftnq
tiS1hbld1WqebBdCSmgl86q80jTpPYuCFEQlAN6lWnK/vDskQGCR/mXBJe1cpkVQ
4UMD7X+k4LVBG24atYqVa5JtHldJqIIfSNFZgL+E7SJiOisTYCKfuuNa1/j9yJVV
UbWmGs95qnhdoq9WdMXfpEVurwKBgQDASVm65gqprWmFgClzLd9uiaOy0avZg8P0
S3Jpq2Cy02V65WCZojVrCJ5GgQ1/oWotU/pRS/4g5xyrFsay/p2F8qXJhoIKtJyH
Oja6pr22CJOBPiIdph1UcXpC0q6+CGVmvUL1r0tUW57RynMCyu+Wv4exkz8tt6rp
jiEyAGORBwKBgBtdHwahwuaKsOypTb7+ATclDB8NlDflx5gY2SNT7N8/TJpdKmJ8
FaPd6N1+DJS2kJtCX5IHQVhVudut/6dYUH28TIAfnCUuehYptMVHIeD57SZ3oe5b
iLoH8WeRq8tPKB72iBDwJO2nHfE5vJMYQjB5RuYOR6r1B6qxV5a0//h1AoGBAJtK
oe0PXA0sv8vRdahPo+LhxhLkwqUohVkGlaBBiBbkI1Ddbuak2f9XNnw6PWyWL+nr
qH1/of1wqPaDrnVgrFdBYCMhPmTm+IM9wHV9tDkPNBFs3KCVR5qrCtJs1DMlFL+k
mi9RIsU+OUW0+q2Gt3hHto7zHFMPwjhdUPHQ3piBAoGBAMX1K1SfekRdgLPuTK8U
1whLYf/PAMgY9kxAoU8BkTfdADzvHsxpFMqsOfmlLqK2dsnTWnw40pMwFSTMCukf
PTuy5JypDwKgH/BB9R6JVYa1G+1qcdDMp17W+sO+s7xA3EbChEyxNY0BsEQhkqtk
5bSAIlxuYPOdhmI5mS9F2u0M
-----END PRIVATE KEY-----
"""

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAlOgkci4anTXlZt13eFKx
woxuA+qpX+XfzYpOFL8Q5v1QON/vuk8Nb34GTvviEUAmkFxd2eBe+h0ercoYwkYM
gTkED0qjis+ilskhXv/z2zxYizwd0LlRDj8o9GkZnZ3kDTayZRwA6oh9gDP5QZVA
Zr38F7t6wiXdOXWTHrkrIUmXby5fZZP/vUysWhD+/wv4MnvOJjtuq3IzsFcz3GFD
Xa0NWK29DrIU95It4vrHMb0MocgjypeYxySZS72MC9B3q0HaTL9mLt/H4BABUAPZ
WT1lMmUMvxpGBunTMY/O4YhMyxnSEpGoEbARk811tVz4rSDULbphlT27p8kwn0Ql
yQIDAQAB
-----END PUBLIC KEY-----
"""

api = BoltAPI()


@api.get("/app-health")
async def health():
    return {"status": "ok"}


@api.get(
    "/whoami",
    auth=[JWTAuthentication(secret=PUBLIC_KEY_PEM, algorithms=["RS256"])],
    guards=[IsAuthenticated()],
)
async def whoami(request):
    return {"user_id": request["context"]["user_id"]}
