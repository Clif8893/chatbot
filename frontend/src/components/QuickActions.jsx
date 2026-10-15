import React from "react";

export default function QuickActions({ actions, onSelect }) {
  return (
    <nav className="quick-actions" aria-label="Suggested questions">
      {actions.map((action) => (
        <button
          key={action.label}
          type="button"
          onClick={() => onSelect(action.message, action.id)}
        >
          {action.label}
        </button>
      ))}
    </nav>
  );
}
