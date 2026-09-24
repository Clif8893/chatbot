export default function QuickActions({ actions, onSelect }) {
  return (
    <nav className="quick-actions" aria-label="Suggested questions">
      {actions.map((action) => (
        <button
          key={action.label}
          type="button"
          onClick={() => onSelect(action.message)}
        >
          {action.label}
        </button>
      ))}
    </nav>
  );
}
