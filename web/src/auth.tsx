import { createContext, useContext, useEffect, useState } from "react";
import { api, tokenStore } from "./api";

interface AuthState {
  email: string | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthState>(null as unknown as AuthState);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!tokenStore.get()) { setReady(true); return; }
    api.me()
      .then((u) => setEmail(u.email))
      .catch(() => tokenStore.clear())
      .finally(() => setReady(true));
  }, []);

  async function login(e: string, p: string) {
    const { access_token } = await api.login(e, p);
    tokenStore.set(access_token);
    const u = await api.me();
    setEmail(u.email);
  }
  async function signup(e: string, p: string) {
    const { access_token } = await api.signup(e, p);
    tokenStore.set(access_token);
    const u = await api.me();
    setEmail(u.email);
  }
  function logout() {
    tokenStore.clear();
    setEmail(null);
  }

  return (
    <Ctx.Provider value={{ email, ready, login, signup, logout }}>
      {children}
    </Ctx.Provider>
  );
}

export const useAuth = () => useContext(Ctx);
