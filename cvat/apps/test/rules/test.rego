package test

import rego.v1

import data.utils

# input: {
#     "scope": <"view"> or null,
#     "auth": {
#         "user": {
#             "id": <num>,
#             "privilege": <"admin"|"user"|"worker"> or null
#         },
#         "organization": { ... } or null,
#     }
#     "resource": null,
# }

default allow := false

allow if {
    utils.is_admin
}

# Phase 1 stub: any authenticated user with at least worker privilege may ping.
# Authentication itself is enforced separately by DRF's IsAuthenticated.
allow if {
    input.scope == utils.VIEW
    utils.has_perm(utils.WORKER)
}
