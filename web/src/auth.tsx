import { createContext, useContext, useEffect, useState } from "react";
import { api, tokenStore } from "./api";

interface AuthState {
  email: string | null;
  isAdmin: boolean;          // 운영자(ADMIN_EMAILS) — /admin 대시보드 게이트
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string) => Promise<void>;
  loginWithGoogle: (credential: string) => Promise<void>;
  loginWithNaver: (code: string, state: string, redirectUri: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthState>(null as unknown as AuthState);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!tokenStore.get()) { setReady(true); return; }
    api.me()
      .then((u) => { setEmail(u.email); setIsAdmin(u.is_admin); })
      .catch(() => tokenStore.clear())
      .finally(() => setReady(true));
  }, []);

  async function login(e: string, p: string) {
    const { access_token } = await api.login(e, p);
    tokenStore.set(access_token);
    const u = await api.me();
    setEmail(u.email); setIsAdmin(u.is_admin);
  }
  async function signup(e: string, p: string) {
    const { access_token } = await api.signup(e, p);
    tokenStore.set(access_token);
    const u = await api.me();
    setEmail(u.email); setIsAdmin(u.is_admin);
  }
  async function loginWithGoogle(credential: string) {
    const { access_token } = await api.googleLogin(credential);
    tokenStore.set(access_token);
    const u = await api.me();
    setEmail(u.email); setIsAdmin(u.is_admin);
  }
  async function loginWithNaver(code: string, state: string, redirectUri: string) {
    const { access_token } = await api.naverLogin(code, state, redirectUri);
    tokenStore.set(access_token);
    const u = await api.me();
    setEmail(u.email); setIsAdmin(u.is_admin);
  }
  function logout() {
    tokenStore.clear();
    setEmail(null); setIsAdmin(false);
  }

  return (
    <Ctx.Provider value={{ email, isAdmin, ready, login, signup, loginWithGoogle, loginWithNaver, logout }}>
      {children}
    </Ctx.Provider>
  );
}

export const useAuth = () => useContext(Ctx);
