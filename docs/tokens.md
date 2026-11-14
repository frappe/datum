# Tokens

Each write carries a JSON Web Token (JWT) in the header `Authorization: Bearer <jwt>`. The token says which resource sends, and Datum writes that resource ID on each row. The sender chooses the metric, the labels, the product, the service and the message, so only `resource_id` says who sent a row.

## Claims

| Claim | Rule |
| --- | --- |
| `resource_id` | Required. The ID that the Beacon registry has for the sender, such as the name of an Atlas Virtual Machine, or `atlas`, `cargo` or `central`. |
| `access` | Must contain `write`. Central signs other tokens with the same key, such as site logins, so Datum takes only a token that says it can write. |
| `iss` | Must be the issuer that the key ID names. |
| `aud` | Must be `atlas-datum:<region id>`. A token for another region does not work here. |
| `iat`, `exp` | Required. Datum refuses an expired token. |

A token without `resource_id` gets `401`. A token without `write` gets `403`.

## Issuers

Datum verifies each token against one key set. The key ID (`kid`) names the issuer, and `iss` must be the same issuer, so a key of one issuer cannot sign as another issuer.

| Issuer | Key ID | `iss` | Signs for |
| --- | --- | --- | --- |
| Central | `central:<id>` | `central` | `central`, and the Pilot of each customer server |
| Atlas | `atlas:<region id>:<id>` | `atlas:<region id>` | `atlas`, the hosts, the service VMs and the Datum agent of the Cargo VM |
| Cargo | `cargo:<region id>:<id>` | `cargo:<region id>` | `cargo`, the Pilot of the Cargo VM, and the object storage and Datum host machines |

Datum takes the resource IDs `central`, `atlas` and `cargo` only from a token of that product. So a machine cannot write as one of the products.

Datum takes only Ed25519 (`EdDSA`) signatures. It refuses HMAC and every other algorithm.

## Key set

`DATUM_JWKS_URL` is the full URL of the key set. Atlas serves the Central keys and its own key at `/api/atlas/jwks.json`. Cargo serves that set and its own key at `/api/method/cargo.api.jwks.get_jwks`. The Datum host that Cargo installs reads the Cargo set.

Datum keeps the keys for 5 minutes. When a fetch fails, Datum keeps the last set it has, so a short outage of the issuer does not refuse every token. A key ID that is not in the set does not make Datum fetch again. Datum keeps no key of its own, so a key rotation needs no change in Datum.

Without `DATUM_JWKS_URL` or `DATUM_REGION_ID`, Datum answers `401` to every request.
