from typing import List, Dict, Optional
from functools import cache

from pydantic import HttpUrl, BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource
)

class Target(BaseModel):
    name: str
    browseable: bool = True
    client: str = "aioboto"
    options: Dict[str,str] = {}


class Settings(BaseSettings):
    """ Settings can be read from a settings.yaml file, 
        or from the environment, with environment variables prepended 
        with "x2s3_" (case insensitive). The environment variables can
        be passed in the environment or in a .env file. 
    """

    log_level: str = 'INFO'
    ui: bool = True
    virtual_buckets: bool = False
    base_url: Optional[HttpUrl] = None
    targets: List[Target] = []
    target_map: Dict[str, Target] = {}

    model_config = SettingsConfigDict(
        yaml_file="config.yaml",
        env_file='.env',
        env_prefix='x2s3_',
        env_nested_delimiter="__",
        env_file_encoding='utf-8'
    )

    def __init__(self, **data) -> None:
        super().__init__(**data)


    def get_target_map(self):
        return {t.name.lower(): t for t in self.targets}


    def get_browseable_targets(self):
        return [target.name for target in self.targets if target.browseable]


    def get_target_config(self, name):
        if name:
            key = name.lower()
            target_map = self.get_target_map()
            if key in target_map:
                return target_map[key]
        return None

  
    @classmethod
    def settings_customise_sources(  # noqa: PLR0913
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


@cache
def get_settings():
    return Settings()
