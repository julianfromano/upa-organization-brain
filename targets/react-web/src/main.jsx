import React from "react";
import { createRoot } from "react-dom/client";
import { Alert, Button, Card, Checkbox, Header, Select, TextField } from "./components.jsx";
import "./app.css";

const COMPONENTS = { Alert, Button, Card, Checkbox, Header, Select, TextField };

function componentFor(ui, role) {
  const name = ui.roles[role]?.[0];
  const component = COMPONENTS[name];
  if (!component) throw new Error(`The React DOM provider cannot render role ${role} (component ${name ?? "not resolved"}).`);
  return component;
}

function Field({ ui, interaction, field, value, onChange }) {
  const id = `${interaction.id}-${field.component_id}`;
  if (field.role === "boolean-input") {
    const Input = componentFor(ui, field.role);
    return <Input id={id} label={field.label} checked={value === true} onChange={(event) => onChange(event.target.checked)} />;
  }
  const Input = componentFor(ui, field.role);
  const props = {
    id,
    label: field.label,
    value,
    onChange: (event) => onChange(event.target.value),
    required: field.required,
  };
  if (field.role === "selection") return <Input {...props} options={field.options ?? []} />;
  return <Input {...props} type={field.input_type ?? "text"} min={field.min} />;
}

function InteractionForm({ ui, interaction }) {
  const [values, setValues] = React.useState({});
  const [message, setMessage] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const fields = interaction.fields.map((field) => ({
    ...field,
    options: field.options ?? [],
  }));

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const response = await fetch(`/api/operations/${encodeURIComponent(interaction.id)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || `Request failed (${response.status}).`);
      setMessage({ severity: "success", text: result.message || "Request completed." });
    } catch (error) {
      setMessage({ severity: "error", text: error.message });
    } finally {
      setBusy(false);
    }
  }

  return <Card title={interaction.title}>
    <form onSubmit={submit}>
      <div className="fields">
        {fields.map((field) => <Field key={field.component_id} ui={ui} interaction={interaction}
          field={field} value={values[field.component_id] ?? (field.role === "boolean-input" ? false : "")}
          onChange={(value) => setValues((current) => ({ ...current, [field.component_id]: value }))} />)}
      </div>
      <div className="actions"><Button type="submit" disabled={busy}>{busy ? "Submitting…" : interaction.title}</Button></div>
    </form>
    {message && <Alert severity={message.severity}>{message.text}</Alert>}
  </Card>;
}

function App() {
  const [ui, setUi] = React.useState(null);
  const [pageId, setPageId] = React.useState(null);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    fetch("/renderable-ui.json")
      .then((response) => {
        if (!response.ok) throw new Error(`Unable to load UI configuration (${response.status}).`);
        return response.json();
      })
      .then((config) => {
        if (config.provider !== "react-web") throw new Error("The UI configuration targets a different provider.");
        setUi(config);
        setPageId(config.pages[0]?.id ?? null);
      })
      .catch((cause) => setError(cause.message));
  }, []);

  if (error) return <main className="app-shell"><Alert severity="error" title="Unable to load the application">{error}</Alert></main>;
  if (!ui) return <main className="app-shell" aria-busy="true">Loading application…</main>;
  const page = ui.pages.find((item) => item.id === pageId);
  const interactions = ui.interactions.filter((item) => item.page_id === pageId);
  return <>
    <Header>
      <nav className="page-tabs" aria-label="Pages">
        {ui.pages.map((item) => <Button key={item.id} onClick={() => setPageId(item.id)}>{item.title}</Button>)}
      </nav>
    </Header>
    <main className="app-shell">
      <h1>{page?.title ?? "Application"}</h1>
      {interactions.length
        ? interactions.map((interaction) => <InteractionForm key={interaction.id} ui={ui} interaction={interaction} />)
        : <p>No interactions are configured for this page.</p>}
    </main>
  </>;
}

createRoot(document.getElementById("root")).render(<App />);
