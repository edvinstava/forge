import { useState } from "react";
import { submitBatch } from "./api";
import { useModelChoices } from "./modelChoices";

interface Props {
  onSubmitted: () => void;
  onClose: () => void;
}

/**
 * Queue a fire-and-forget batch: pick a repo, paste one task per line, and each
 * line becomes a queued run on that repo. POST /api/batch never 409s — over-
 * capacity items wait in the queue and drain as slots free.
 */
export function BatchComposer({ onSubmitted, onClose }: Props) {
  const modelChoices = useModelChoices();
  const [repo, setRepo] = useState("");
  const [tasksText, setTasksText] = useState("");
  const [model, setModel] = useState<string>("auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tasks = tasksText.split("\n").map((t) => t.trim()).filter(Boolean);
  const canSubmit = repo.trim().length > 0 && tasks.length > 0 && !busy;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const items = tasks.map((task) => ({ repo: repo.trim(), task, model }));
      const res = await submitBatch(items);
      if (!res.run_ids) throw new Error("batch rejected");
      setTasksText("");
      onSubmitted();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="batch-composer">
      <div className="batch-composer-header">
        <span className="repo-picker-title">queue a batch</span>
        <button className="repo-picker-close" onClick={onClose} title="Close">✕</button>
      </div>
      <input
        className="input"
        type="text"
        placeholder="owner/repo"
        value={repo}
        onChange={(e) => setRepo(e.target.value)}
      />
      <textarea
        className="input batch-composer-tasks"
        rows={6}
        placeholder="one task per line…"
        value={tasksText}
        onChange={(e) => setTasksText(e.target.value)}
      />
      <div className="batch-composer-row">
        <select className="input" value={model} onChange={(e) => setModel(e.target.value)}>
          {modelChoices.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
        <button className="btn btn-accent" disabled={!canSubmit} onClick={submit}>
          {busy ? "queuing…" : `queue ${tasks.length || ""} task${tasks.length === 1 ? "" : "s"}`}
        </button>
      </div>
      {error && <div className="batch-composer-error">{error}</div>}
    </div>
  );
}
