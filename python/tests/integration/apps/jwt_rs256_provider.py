"""App under test for asymmetric (RS256) JWT auth over a real server.

Routes are protected by ``JWTAuthentication(public_key=..., algorithms=["RS256"])``
so a real ``runbolt`` server proves the PEM public key survives startup wiring
(Python metadata → Rust decoding key built at registration) and verifies
provider-style tokens — including ones with non-string JOSE header extension
parameters, as emitted by Clerk (``"oiat": <int>``) and Auth0 (``"gty": [...]``).

The keypair is static (not generated at import time) because this module is
imported independently by both the test process and the server subprocess —
generated keys would differ between the two.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCjolygIL9hf/mC
8bBFsrpSYSVtRqG05eZ3jGktrfXkw+XveTOj1l6ewYTga5UZ0kx7bnXeXWJdQNWY
HZl7poZya+rs+ECRMzbOCvctI5drmJQARSZJefF3ZRpnr3z8rwXZhmPDKNjubWKi
oabnB3ldaLK/wnVRTfTDtlQlyHyo2TVo9yXDVWgfYzqypDLQTjNdfYpY8J8wIT1y
qwZzV7GZTj+O2BMxD6PSqCNYVpVhvnOimbaa3296iZELzJP5SLSRAYRqhKCfYOUb
TVqmVn9WGL8y+A+vO3Q9uOJnUd3s3DA7lOR+GH0M99h0Rj+d+JzEUw5jlDjyLNGy
nRqvukqpAgMBAAECggEAA7QgZedtLXxuCKQGb1gU64RotR53eSwIRAXDjlXKTlE4
xRYjMGl2XsrWgmss8WwBxdFsODTjjbodRizKqofMXI6gv0L8gyImSBJAzJ6/m0nV
dQ7GR7zLgFOjfgafd5X74JPax4dcfEOxiyjJfvUmWWeN6X/ck+iFymA4FgWBp3ip
04ry3ORLItciTr9tUYXLqRN1iwBYNAqf1VIuCb4KYXFpCPgz1j3cXvvrewdGNEU9
pe4eK2FjHCwBOAhG7J9aWmzVymQV0UIBardTulrGjV2K6hDOHsAnJgGRMJm5NHUR
iU1IYjEB2cBnV/r9QQ502SeYV7JydhWBmoq+ab4UuQKBgQDR6FPpoQYvLU5x2Vaq
OrrqAZxlvYXJ0J1zCbTrJzNojtNyJMEbNSBF9ZlNJoSIIYM4V3Zi54ghI1e3xdF4
wdpvY8ObXERZKoCnB7KCOxCE/d2BCr0WDhIqRh71S83ZWR5GHQIlfk4/u8ogAa4I
4UMdKsTjEY07UZKGUzoXRTC3/wKBgQDHkNeD9OSPgJbUfWs047yAqmJ5cGfdPs+i
YUAVU/O127AbIHhlq8sEqgmxrFcS95yVS6N7e0UoVUbHbumuyx1yjBK6IZoaH2t1
hi//IgK+YETb9362DZy1LA72SHXI1O54Jl7o+Gqe3mx3ms6JkdfNNv2hYAr9iSMI
+hPOD6w9VwKBgEB/7Vj629WfTF17dT/1r/275Pz2UagD7H2u2+LuNsPIL30Bgj0E
BBi7MRId5deWxKWJap1Vm+Ti4U9c/9LlbmOP+klA/tePUd0BZn7R+2+COpAuZo/i
Xv6ScWzakDRbSAwvWbt/pje7Uo6nNX0RCvhpbfqAKC+0Dxwrcsw3vJKtAoGAMobw
mf9aEx86kQhEKXrzkhwRnK+iDHlHttQqlnvP+55owyWAdjV9zGuE0tBQp4O7yG0D
MlNumhylM/9X+SKCDSt73lZ9ntmPqozUACPLUAotxQevtEZUA+bozuBfuf53dkI4
y4GB9UFZcxrl6hzb56BhrQcVIUYkcbRnaUe8kzcCgYA8O/8DQGRp/SzWsQSKi3f4
V/aysAzGZNB2VNHS8SygYCSrdBqhuA8bH9lYS4tOBN9N3++2YLocQSkWCVHjhJMX
/7n+vx+LsZlqM8tqNfJUVC1VpFGVycp6QhU9k6jGsSJ/zbRTexhiwxy2/es06NTZ
3TuAtqGZERaYeh5l+2Q5YQ==
-----END PRIVATE KEY-----
"""

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAo6JcoCC/YX/5gvGwRbK6
UmElbUahtOXmd4xpLa315MPl73kzo9ZensGE4GuVGdJMe2513l1iXUDVmB2Ze6aG
cmvq7PhAkTM2zgr3LSOXa5iUAEUmSXnxd2UaZ698/K8F2YZjwyjY7m1ioqGm5wd5
XWiyv8J1UU30w7ZUJch8qNk1aPclw1VoH2M6sqQy0E4zXX2KWPCfMCE9cqsGc1ex
mU4/jtgTMQ+j0qgjWFaVYb5zopm2mt9veomRC8yT+Ui0kQGEaoSgn2DlG01aplZ/
Vhi/MvgPrzt0PbjiZ1Hd7NwwO5Tkfhh9DPfYdEY/nficxFMOY5Q48izRsp0ar7pK
qQIDAQAB
-----END PUBLIC KEY-----
"""

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get(
    "/whoami",
    auth=[JWTAuthentication(public_key=PUBLIC_KEY_PEM, algorithms=["RS256"])],
    guards=[IsAuthenticated()],
)
async def whoami(request):
    return {"user_id": request["context"]["user_id"]}
