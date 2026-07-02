import { useEffect, useState } from "react";
import { MODEL_CHOICES, type ProxyConfig } from "./types";
import { getConfigCached } from "./api";

/** The provider-specific model list from /api/config, falling back to the
 * static claude aliases when the config is missing or malformed. */
export function modelChoicesFrom(
  cfg: Pick<ProxyConfig, "model_choices"> | null | undefined,
): string[] {
  const list = cfg?.model_choices;
  return Array.isArray(list) && list.length > 0 ? list.map(String) : MODEL_CHOICES;
}

/** Server-driven model choices for the pickers. Renders the fallback until
 * /api/config answers; a fetch failure just keeps the fallback. */
export function useModelChoices(): string[] {
  const [choices, setChoices] = useState<string[]>(MODEL_CHOICES);
  useEffect(() => {
    let alive = true;
    getConfigCached()
      .then((c) => { if (alive) setChoices(modelChoicesFrom(c)); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  return choices;
}
