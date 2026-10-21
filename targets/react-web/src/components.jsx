export function Button({ children, type = "button", disabled = false, onClick }) {
  return <button className="button" type={type} disabled={disabled} onClick={onClick}>{children}</button>;
}

export function Card({ title, children }) {
  return <section className="card"><h2>{title}</h2>{children}</section>;
}

export function TextField({ id, label, type = "text", value, onChange, required = false, min }) {
  return <label className="field" htmlFor={id}>{label}
    <input id={id} type={type} value={value} onChange={onChange} required={required}
      {...(min !== undefined && min !== null ? { min } : {})} />
  </label>;
}

export function Checkbox({ id, label, checked, onChange }) {
  return <label className="checkbox" htmlFor={id}>
    <input id={id} type="checkbox" checked={checked} onChange={onChange} />{label}
  </label>;
}

export function Select({ id, label, options = [], value, onChange, required = false }) {
  return <label className="field" htmlFor={id}>{label}
    <select id={id} value={value} onChange={onChange} required={required}>
      <option value="">Choose an option</option>
      {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select>
  </label>;
}

export function Alert({ severity = "info", title, children }) {
  return <div className={`alert alert-${severity}`} role={severity === "error" ? "alert" : "status"}>
    {title && <strong>{title}</strong>}{children && <div>{children}</div>}
  </div>;
}

export function Header({ children }) {
  return <header className="site-header">{children}</header>;
}

export function Toast({ severity = "info", children }) {
  return <Alert severity={severity}>{children}</Alert>;
}
