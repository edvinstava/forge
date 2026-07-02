import { describe, expect, it } from "vitest";
import { modelChoicesFrom } from "./modelChoices";
import { MODEL_CHOICES } from "./types";

describe("modelChoicesFrom", () => {
  it("uses the server-provided provider list", () => {
    expect(
      modelChoicesFrom({ model_choices: ["auto", "gpt-5-codex", "gpt-5"] }),
    ).toEqual(["auto", "gpt-5-codex", "gpt-5"]);
  });

  it("falls back to the static aliases when config is absent or malformed", () => {
    expect(modelChoicesFrom(null)).toEqual(MODEL_CHOICES);
    expect(modelChoicesFrom({})).toEqual(MODEL_CHOICES);
    expect(modelChoicesFrom({ model_choices: [] })).toEqual(MODEL_CHOICES);
    expect(modelChoicesFrom({ model_choices: "opus" as any })).toEqual(MODEL_CHOICES);
  });
});
