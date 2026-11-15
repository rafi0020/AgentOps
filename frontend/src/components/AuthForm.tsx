"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api, ApiError, setCsrf } from "@/lib/api";

const DEMO = [
  ["Engineer", "engineer@lumen.demo", "Engineer#Demo2026"],
  ["Administrator", "admin@lumen.demo", "Admin#Demo2026"],
  ["Viewer", "viewer@lumen.demo", "Viewer#Demo2026"],
];

/** Sign-in and sign-up as a terminal session: the form is centred, the details are monospace. */
export function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const [form, setForm] = useState({ email: "", password: "", name: "", org_name: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = mode === "login" ? { email: form.email, password: form.password } : form;
      const result = await api<{ csrf_token: string }>(`/api/auth/${mode}`, { method: "POST", json: body });
      setCsrf(result.csrf_token);
      window.location.href = "/overview/";
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong.");
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <section className="term" aria-label={mode === "login" ? "Sign in" : "Create an organisation"}>
        <header className="term__bar">
          <span>agentops-control-plane</span>
          <span>{mode === "login" ? "sign-in" : "sign-up"}</span>
        </header>
        <div className="term__body">
          <p className="term__line"><span className="term__prompt">$</span>AgentOps Control Plane</p>
          <p className="term__line"><span className="term__prompt">#</span>traces, evaluations, prompt releases and an investigator for LLM applications</p>
          <h1>{mode === "login" ? "Sign in" : "Create your organisation"}</h1>
          <form onSubmit={submit} noValidate>
            {mode === "signup" && (
              <>
                <label className="field"><span className="label">Organisation name</span><input className="input" required value={form.org_name} onChange={set("org_name")} autoComplete="organization" /></label>
                <label className="field"><span className="label">Your name</span><input className="input" required value={form.name} onChange={set("name")} autoComplete="name" /></label>
              </>
            )}
            <label className="field"><span className="label">E-mail</span><input className="input measured" type="email" required value={form.email} onChange={set("email")} autoComplete="email" aria-invalid={!!error} /></label>
            <label className="field">
              <span className="label">Password</span>
              <input className="input measured" type="password" required value={form.password} onChange={set("password")} autoComplete={mode === "login" ? "current-password" : "new-password"} aria-invalid={!!error} />
              {mode === "signup" && <span className="hint">At least 10 characters, with a letter and a number.</span>}
            </label>
            {error && <p className="hint hint--bad" role="alert">{error}</p>}
            <button className="btn btn--primary" disabled={busy} aria-busy={busy}>{mode === "login" ? "Sign in" : "Create organisation"}</button>
          </form>
          <p className="term__line">
            {mode === "login" ? <>New here? <Link href="/signup/">Create an organisation</Link></> : <>Already have an account? <Link href="/login/">Sign in</Link></>}
          </p>
        </div>
        {mode === "login" && (
          <div className="term__accounts">
            <table className="table">
              <thead><tr><th colSpan={2}>Demo accounts, Lumen Labs, generated data</th><th /></tr></thead>
              <tbody>
                {DEMO.map(([role, email, password]) => (
                  <tr key={email}>
                    <td className="muted">{role}</td>
                    <td className="measured">{email}</td>
                    <td className="num"><button type="button" className="btn btn--sm" onClick={() => setForm({ ...form, email, password })}>Fill in</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
