import { useState } from "react";
import type { ImportPreviewResult } from "../lib/types";

interface Props {
  file: File;
  preview: ImportPreviewResult;
  onCancel: () => void;
  onConfirm: (columnMap: Record<string, string>) => void;
  importing: boolean;
}

// Column naming isn't guaranteed to stay identical across future ad-platform
// exports (§3) -- rather than silently guessing or failing deep in the row
// loop, this surfaces every expected field alongside whatever column
// auto-matched it in *this* file, so staff can confirm it's right or pick
// the actual column before a single row is imported.
export default function ImportColumnMappingModal({ file, preview, onCancel, onConfirm, importing }: Props) {
  const [mapping, setMapping] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const f of preview.fields) {
      if (f.matched_column) initial[f.field] = f.matched_column;
    }
    return initial;
  });

  const missingRequired = preview.fields.filter((f) => f.required && !mapping[f.field]);

  const handleConfirm = () => {
    if (missingRequired.length > 0) return;
    onConfirm(mapping);
  };

  return (
    <div className="panel-backdrop" onClick={onCancel}>
      <div className="import-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Confirm column mapping</h2>
        <p className="import-modal__hint">
          "{file.name}" — for each field below, confirm the column we matched automatically, or pick the right one
          if the naming's different in this file.
        </p>

        <div className="import-modal__fields">
          {preview.fields.map((f) => (
            <label key={f.field} className="import-modal__field">
              <span className="import-modal__field-label">
                {f.label}
                {f.required && <span className="import-modal__required"> · required</span>}
              </span>
              <select
                value={mapping[f.field] ?? ""}
                onChange={(e) => setMapping((prev) => ({ ...prev, [f.field]: e.target.value }))}
                className={f.required && !mapping[f.field] ? "import-modal__select--missing" : ""}
              >
                <option value="">— not in file —</option>
                {preview.columns.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>

        {missingRequired.length > 0 && (
          <p className="import-modal__warning">
            Missing required: {missingRequired.map((f) => f.label).join(", ")} — pick a column for each before
            importing.
          </p>
        )}

        <div className="button-row" style={{ marginTop: 20, marginBottom: 0 }}>
          <button onClick={onCancel} disabled={importing}>
            Cancel
          </button>
          <button onClick={handleConfirm} disabled={importing || missingRequired.length > 0} className="primary">
            {importing ? "Importing…" : "Confirm & import"}
          </button>
        </div>
      </div>
    </div>
  );
}
