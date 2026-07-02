import { useState } from "react";
import type { PlanData, CheckpointData, CheckpointAction } from "./types";

interface PlanCardProps {
  plan: PlanData | null;
  checkpoint: CheckpointData;
  disabled?: boolean;
  onRespond: (action: CheckpointAction, body: string) => void;
}

export function PlanCard({ plan, checkpoint, disabled, onRespond }: PlanCardProps) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const isEscalation = checkpoint.type === "repair_escalation";
  const isAmbiguity = checkpoint.type === "ambiguity";

  const submit = (action: CheckpointAction) => {
    onRespond(action, text.trim());
    setText("");
    setEditing(false);
  };

  return (
    <div className={`plan-card plan-card--${checkpoint.type}`}>
      {plan && !isEscalation && (
        <>
          <div className="plan-card-head">
            <span className="plan-card-title">Plan</span>
            <span className={`plan-card-risk plan-card-risk--${plan.risk}`}>risk: {plan.risk}</span>
          </div>
          <div className="plan-card-goal">{plan.goal}</div>
          {plan.steps.length > 0 && (
            <ol className="plan-card-steps">
              {plan.steps.map((s, i) => <li key={i}>{s}</li>)}
            </ol>
          )}
          {plan.acceptance.length > 0 && (
            <div className="plan-card-section">
              <span className="plan-card-label">Acceptance</span>
              <ul>{plan.acceptance.map((a, i) => <li key={i}>{a}</li>)}</ul>
            </div>
          )}
          {plan.open_questions.length > 0 && (
            <div className="plan-card-section plan-card-section--questions">
              <span className="plan-card-label">Open questions</span>
              <ul>{plan.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
            </div>
          )}
        </>
      )}

      {isEscalation && (
        <div className="plan-card-head">
          <span className="plan-card-title">Needs your input</span>
        </div>
      )}

      <div className="plan-card-prompt">{checkpoint.prompt}</div>

      {editing ? (
        <div className="plan-card-edit">
          <textarea
            className="plan-card-textarea"
            placeholder={isAmbiguity ? "Answer the open questions…" : "Describe the changes you want…"}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={3}
            autoFocus
          />
          <div className="plan-card-actions">
            <button className="btn btn-accent" disabled={disabled || !text.trim()}
              onClick={() => submit("edit")}>
              {isEscalation ? "Retry with guidance" : isAmbiguity ? "Answer & re-plan" : "Submit changes"}
            </button>
            <button className="btn" disabled={disabled} onClick={() => { setEditing(false); setText(""); }}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="plan-card-actions">
          <button className="btn btn-accent" disabled={disabled} onClick={() => submit("approve")}>
            {isEscalation ? "Retry as-is" : isAmbiguity ? "Proceed anyway" : "Approve"}
          </button>
          <button className="btn" disabled={disabled} onClick={() => setEditing(true)}>
            {isEscalation ? "Add guidance" : "Request changes"}
          </button>
          <button className="btn btn-danger" disabled={disabled} onClick={() => submit("reject")}>
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
