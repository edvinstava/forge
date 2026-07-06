import { useState, useEffect, useCallback } from "react";
import type { VerifyResult } from "./types";
import { getVerify } from "./api";

export function VerifyView({ activeId }: { activeId: string }) {
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setResult(await getVerify(activeId));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [activeId]);

  useEffect(() => {
    load();
  }, [load]);

  const ok = result?.verify_ok ?? null;
  const tone = ok === true ? "pass" : ok === false ? "fail" : "none";
  const label = ok === true ? "passing" : ok === false ? "failing" : "no checks";
  const sub =
    ok === true
      ? "all configured checks passed"
      : ok === false
      ? "one or more checks failed"
      : "this repo has no verification configured";

  return (
    <div className="inspector-pane">
      <div className="inspector-toolbar">
        <span className="inspector-section-label">verify</span>
        <button className="btn btn-sm" onClick={load} disabled={loading}>
          {loading ? "…" : "↻"}
        </button>
      </div>

      {error && <div className="inspector-error">{error}</div>}

      {!loading && !error && result && (
        <div className="verify-body scrollable">
          <div className={`verify-card verify-card--${tone}`}>
            <div className="verify-icon">{ok === true ? "✓" : ok === false ? "✕" : "—"}</div>
            <div className="verify-text">
              <div className="verify-label">{label}</div>
              <div className="verify-sub">{sub}</div>
            </div>
          </div>

          <div className="verify-meta">
            {result.diff_files != null && (
              <div className="verify-meta-item">
                <span className="verify-meta-k">files changed</span>
                <span className="verify-meta-v">{result.diff_files}</span>
              </div>
            )}
            {result.model && (
              <div className="verify-meta-item">
                <span className="verify-meta-k">model</span>
                <span className="verify-meta-v">⚡ {result.model}</span>
              </div>
            )}
          </div>

          {result.verify_failed.length > 0 && (
            <div className="verify-failed">
              <span className="inspector-section-label">failed checks</span>
              <div className="verify-failed-chips">
                {result.verify_failed.map((name) => (
                  <span key={name} className="verify-chip">
                    {name}
                  </span>
                ))}
              </div>
            </div>
          )}

          {result.verify_output && (
            <div className="verify-output-wrap">
              <span className="inspector-section-label">output</span>
              <pre className="verify-output scrollable">{result.verify_output}</pre>
            </div>
          )}
        </div>
      )}

      {!loading && !error && !result && (
        <div className="inspector-empty">
          <div className="inspector-empty-icon">—</div>
          <div className="inspector-empty-label">no verify data</div>
        </div>
      )}
    </div>
  );
}
