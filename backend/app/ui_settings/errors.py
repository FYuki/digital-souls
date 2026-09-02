class UiSettingsError(ValueError):
    pass


class UiCharacterNotAddedError(UiSettingsError):
    pass


class UiThreadNotFoundError(UiSettingsError):
    pass
