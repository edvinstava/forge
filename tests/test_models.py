from forge.models import auto_model, resolve_model, MODEL_CHOICES


def test_explicit_choice_is_passed_through():
    assert resolve_model("opus", "anything") == "opus"
    assert resolve_model("sonnet", "anything") == "sonnet"
    assert resolve_model("haiku", "anything") == "haiku"


def test_auto_picks_opus_for_heavy_tasks():
    assert resolve_model("auto", "Implement an error boundary for the app") == "opus"
    assert auto_model("Refactor the session manager") == "opus"
    assert auto_model("debug the failing health check race condition") == "opus"


def test_auto_picks_haiku_for_trivial_short_tasks():
    assert auto_model("fix a typo in the readme") == "haiku"
    assert auto_model("rename the variable foo to bar") == "haiku"


def test_auto_defaults_to_sonnet_for_ordinary_tasks():
    assert auto_model("make the header bold") == "sonnet"
    assert resolve_model("auto", "add a column to the table") == "sonnet"


def test_unknown_or_empty_choice_falls_back_to_auto():
    # An unknown/blank choice must not break the run — treat it as auto.
    assert resolve_model("", "implement a feature") == "opus"
    assert resolve_model(None, "make the header bold") == "sonnet"
    assert resolve_model("bogus-model", "fix a typo") == "haiku"


def test_model_choices_are_advertised_for_the_ui():
    # The UI builds its picker from this list; auto must be first (the default).
    assert MODEL_CHOICES[0] == "auto"
    assert set(MODEL_CHOICES) == {"auto", "opus", "sonnet", "haiku"}
