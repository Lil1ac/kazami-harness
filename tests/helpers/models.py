"""测试专用模型目录。所有实际请求由离线替身或本机测试服务处理。"""

from agent_harness.schemas import RunConfig


MODEL_TEST_TOML = '''
[custom_providers.custom-fixture]
name = "测试连接"
base_url = "https://fixture.example.test/v1"
default_model = "fixture-model"
api_key_env = "HARNESS_TEST_MODEL_KEY"
'''  # CLI 和后台进程测试登记的离线连接。


def configured_run(*, local: bool = False, **overrides) -> RunConfig:
    """返回两个测试端点；local 为真使用免密回环，overrides 覆盖主模型或其他策略。"""
    provider = overrides.get("provider", "custom-fixture")
    definitions = {
        "custom-fixture": {"name": "测试连接", "base_url": "https://fixture.example.test/v1",
                           "default_model": "fixture-model", "api_key_env": "HARNESS_TEST_MODEL_KEY"},
        "custom-secondary": {"name": "测试连接 B", "base_url": "https://secondary.example.test/v1",
                             "default_model": "fixture-review", "api_key_env": "HARNESS_TEST_MODEL_KEY"},
    }
    if "requested_model" in overrides:
        definitions[provider]["default_model"] = overrides["requested_model"]
    if local:
        for definition in definitions.values():
            definition.update(base_url="http://127.0.0.1/v1", api_key_env=None, auth="none")
    return RunConfig(**{"provider": provider, "custom_providers": definitions, **overrides})
