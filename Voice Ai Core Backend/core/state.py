# ================================================================
# FILE EXECUTION FLOW
# ================================================================
#
# [ DATA SOURCE — no methods defined in this file ]
#
# Shared dict _m populated by lifespan() at startup.
# Consumed by:
#     |----> stt_sync()               * reads _m["stt"]
#     |----> _gemini_sync()           * reads _m["gemini"]
#     |----> _build_final_system()    * reads _m["company_context"]
#     |----> _build_qwen_system()     * reads _m["company_context"]
#
# ================================================================

from typing import Dict

_m: Dict = {}
