#!/usr/bin/env bash
set -euo pipefail

api_image="${PILOT107_API_IMAGE:-pilot107/api:local}"
worker_image="${PILOT107_WORKER_IMAGE:-pilot107/worker:local}"
web_image="${PILOT107_WEB_IMAGE:-pilot107/web:local}"

for image in "$api_image" "$worker_image" "$web_image"; do
  docker run --rm "$image" python3 - <<'PY'
import pilot107
import yaml
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.service import config_from_env as api_config_from_env
from pilot107.core.contracts import RecipeCatalog
from pilot107.core.diagnosis import load_known_error_rules
from pilot107.web.server import WebConfig, WebIdentityMode, config_from_env as web_config_from_env
from pilot107.worker.service import config_from_env as worker_config_from_env

assert pilot107 is not None
assert yaml.__version__
assert Pilot107HttpApi is not None
recipe_summaries = RecipeCatalog().list_summaries()
assert recipe_summaries[0].recipe_id == "recipe_python_cpu"
assert len(recipe_summaries) >= 3
assert load_known_error_rules()
assert api_config_from_env({"PILOT107_API_BACKEND": "demo"}).backend == "demo"
assert worker_config_from_env({"PILOT107_WORKER_BACKEND": "demo"}).backend == "demo"
assert web_config_from_env({"PILOT107_WEB_API_BASE_URL": "http://api:8080"}).api_base_url == "http://api:8080"
fixed_web = web_config_from_env({"PILOT107_WEB_IDENTITY_MODE": "fixed_user", "PILOT107_WEB_FIXED_USER": "alice"})
assert fixed_web.identity_mode == WebIdentityMode.FIXED_USER
assert fixed_web.fixed_user == "alice"
assert WebConfig(api_base_url="http://api:8080").demo_user == "alice"
PY
done

echo "pilot107 app images ok"
