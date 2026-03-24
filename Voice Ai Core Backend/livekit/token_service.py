# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ START ]
#    |
#    v
# +-------------------------------+
# | generate_token()              |
# | * build signed LiveKit JWT    |
# +-------------------------------+
#    |
#    |----> <AccessToken> -> AccessToken()       * init with key secret
#    |
#    |----> <AccessToken> -> with_identity()     * set participant id
#    |
#    |----> <AccessToken> -> with_name()         * set display name
#    |
#    |----> <VideoGrants> -> VideoGrants()       * set room permissions
#    |
#    |----> <AccessToken> -> with_grants()       * attach permissions
#    |
#    |----> <AccessToken> -> to_jwt()            * sign serialize token
#    |
#    v
#    [ RETURN signed JWT string ]
#
# ================================================================

import os

LIVEKIT_URL        = os.getenv("LIVEKIT_URL",        "ws://localhost:7880")
LIVEKIT_API_KEY    = os.getenv("LIVEKIT_API_KEY",    "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "devsecret")


def generate_token(
    room_name:     str,
    identity:      str,
    name:          str  = "",
    *,
    can_publish:   bool = True,
    can_subscribe: bool = True,
) -> str:
    from livekit.api import AccessToken, VideoGrants

    grants = VideoGrants(
        room_join    = True,
        room         = room_name,
        can_publish  = can_publish,
        can_subscribe= can_subscribe,
    )

    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name or identity)
        .with_grants(grants)
        .to_jwt()
    )
    return token
