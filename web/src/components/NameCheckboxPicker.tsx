export function NameCheckboxPicker({
  id,
  available,
  selected,
  onChange,
  emptyLabel,
}: {
  id: string;
  available: Array<{ name: string; description?: string | null }>;
  selected: string[];
  onChange: (names: string[]) => void;
  emptyLabel: string;
}) {
  const availableNames = new Set(available.map((item) => item.name));
  const selectedNames = new Set(selected);
  const orphaned = selected.filter((name) => !availableNames.has(name));
  const options = [...orphaned.map((name) => ({ name, description: "" })), ...available];

  if (options.length === 0) {
    return <p className="text-xs text-muted-foreground">{emptyLabel}</p>;
  }

  const toggle = (name: string, checked: boolean) => {
    onChange(checked ? [...selected, name] : selected.filter((item) => item !== name));
  };

  return (
    <div id={id} className="max-h-36 overflow-y-auto border border-border bg-background/40 p-1">
      {options.map((item) => (
        <label
          key={item.name}
          className="flex cursor-pointer items-center gap-2 px-2 py-1 text-xs hover:bg-muted/40"
          title={item.description || undefined}
        >
          <input
            type="checkbox"
            className="accent-foreground"
            checked={selectedNames.has(item.name)}
            onChange={(event) => toggle(item.name, event.target.checked)}
          />
          <span className="font-mono-ui truncate">{item.name}</span>
        </label>
      ))}
    </div>
  );
}
