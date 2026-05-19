import { useState } from "react";
import { useAuth } from "../auth";

export default function Login() {
  const { login, signup } = useAuth();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      if (mode === "login") await login(email, pw);
      else await signup(email, pw);
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="center-wrap">
      <div className="panel auth-box">
        <div className="brand" style={{ padding: "0 0 18px" }}>
          퀀트<span>플랫폼</span>
        </div>
        <p className="page-sub">
          {mode === "login" ? "로그인하고 전략을 관리하세요." : "계정을 만들어 시작하세요."}
        </p>
        <form onSubmit={submit}>
          <div className="field">
            <label>이메일</label>
            <input
              type="email" value={email} required
              style={{ width: "100%" }}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label>비밀번호</label>
            <input
              type="password" value={pw} required minLength={6}
              style={{ width: "100%" }}
              onChange={(e) => setPw(e.target.value)}
            />
          </div>
          {err && <div className="error">{err}</div>}
          <button type="submit" disabled={busy} style={{ width: "100%" }}>
            {busy ? "처리 중…" : mode === "login" ? "로그인" : "회원가입"}
          </button>
        </form>
        <div className="spacer" />
        <div className="muted" style={{ textAlign: "center" }}>
          {mode === "login" ? "계정이 없으신가요? " : "이미 계정이 있으신가요? "}
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              setErr("");
              setMode(mode === "login" ? "signup" : "login");
            }}
          >
            {mode === "login" ? "회원가입" : "로그인"}
          </a>
        </div>
      </div>
    </div>
  );
}
