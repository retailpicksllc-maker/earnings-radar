// Earnings Radar — drop-in sign in / sign up popup.
// Include on any page with:  <script type="module" src="assets/auth.js"></script>
// Triggers: an element with id="authTrigger", or any element with [data-auth-open].
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import {
  getAuth, onAuthStateChanged, createUserWithEmailAndPassword, signInWithEmailAndPassword,
  signOut, updateProfile, GoogleAuthProvider, signInWithPopup, sendPasswordResetEmail
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";

// Same Firebase project as the iOS app.
// NOTE: add your Web app's appId from Firebase Console → Project settings → Your apps → Web.
// Email/password works without it.
const firebaseConfig = {
  apiKey: "AIzaSyCkYi_67SreyN6FG7oxrHLypOP0eOC3Efs",
  authDomain: "earnings-radar-f398e.firebaseapp.com",
  projectId: "earnings-radar-f398e",
  appId: "1:621874989792:web:348614b693e5d261b45e85"
};
const auth = getAuth(initializeApp(firebaseConfig));

/* ---------- styles (scoped, self-contained) ---------- */
const css = `
.erAuth{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;padding:18px;
  background:rgba(4,6,15,.72);backdrop-filter:blur(5px)}
.erAuth.on{display:flex}
.erAuth *{box-sizing:border-box}
.erAuth .box{width:100%;max-width:400px;background:#0d1430;border:1px solid rgba(255,255,255,.12);border-radius:20px;
  padding:24px;color:#ECECF1;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  box-shadow:0 30px 80px rgba(0,0,0,.6)}
.erAuth h3{font-size:22px;font-weight:900;letter-spacing:-.4px;margin:0}
.erAuth .close{position:absolute;top:14px;right:16px;width:32px;height:32px;border-radius:9px;border:1px solid rgba(255,255,255,.12);
  background:rgba(255,255,255,.05);color:#ECECF1;font-size:15px;cursor:pointer}
.erAuth .tabs{display:flex;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:4px;margin:16px 0}
.erAuth .tabs button{flex:1;padding:10px;border:0;background:none;color:#9AA4C2;font-weight:800;font-size:14px;border-radius:9px;cursor:pointer}
.erAuth .tabs button.active{background:rgba(255,255,255,.08);color:#ECECF1}
.erAuth label{font-size:11.5px;font-weight:800;letter-spacing:.4px;color:#9AA4C2;text-transform:uppercase;display:block;margin:0 0 5px}
.erAuth input{width:100%;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:11px;
  padding:12px 13px;color:#ECECF1;font-size:15px;outline:none;margin-bottom:12px}
.erAuth input:focus{border-color:#2F6BFF}
.erAuth .primary{width:100%;padding:13px;border:0;border-radius:12px;font-weight:800;font-size:15px;color:#fff;cursor:pointer;
  background:linear-gradient(135deg,#2F6BFF,#1547C4);box-shadow:0 8px 22px rgba(47,107,255,.4)}
.erAuth .primary:disabled{opacity:.6}
.erAuth .gbtn{width:100%;display:flex;align-items:center;justify-content:center;gap:10px;padding:11px;border:0;border-radius:12px;
  background:#fff;color:#1f2430;font-weight:800;font-size:15px;cursor:pointer;margin-top:2px}
.erAuth .divider{display:flex;align-items:center;gap:12px;color:#9AA4C2;font-size:12px;margin:14px 0}
.erAuth .divider::before,.erAuth .divider::after{content:"";flex:1;height:1px;background:rgba(255,255,255,.1)}
.erAuth .msg{font-size:13px;font-weight:700;min-height:18px;margin-top:10px}
.erAuth .msg.err{color:#F87171}.erAuth .msg.ok{color:#34D399}
.erAuth .forgot{color:#6BA5FF;font-weight:700;font-size:12.5px;cursor:pointer;display:inline-block;margin-top:10px}
.erAuth .foothint{color:#9AA4C2;font-size:12px;text-align:center;margin-top:14px}
.erAuth .acct{text-align:center}
.erAuth .av{width:60px;height:60px;border-radius:50%;background:linear-gradient(135deg,#2F6BFF,#1547C4);display:flex;align-items:center;
  justify-content:center;font-size:24px;font-weight:900;color:#fff;margin:6px auto 12px}
.erAuth .acct .em{color:#9AA4C2;font-size:14px;margin-top:2px}
.erAuth .acct .row{display:grid;gap:10px;margin-top:20px}
.erAuth .ghost{width:100%;padding:12px;border:1px solid rgba(255,255,255,.12);border-radius:12px;background:rgba(255,255,255,.05);
  color:#ECECF1;font-weight:800;font-size:15px;cursor:pointer}`;
const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);

/* ---------- markup ---------- */
const root = document.createElement("div");
root.className = "erAuth";
root.innerHTML = `
  <div class="box" style="position:relative">
    <button class="close" data-close aria-label="Close">✕</button>

    <div data-view="form">
      <h3 data-title>Welcome back</h3>
      <div class="tabs">
        <button data-tab="signin" class="active" type="button">Sign in</button>
        <button data-tab="signup" type="button">Create account</button>
      </div>
      <form data-form>
        <div data-namewrap style="display:none">
          <label>Name</label>
          <input type="text" data-name autocomplete="name" placeholder="Alex Rivera" />
        </div>
        <label>Email</label>
        <input type="email" data-email autocomplete="email" placeholder="you@example.com" required />
        <label>Password</label>
        <input type="password" data-password autocomplete="current-password" placeholder="••••••••" required />
        <button class="primary" type="submit" data-submit>Sign in</button>
        <div class="msg" data-msg></div>
        <span class="forgot" data-forgot>Forgot password?</span>
      </form>
      <div class="divider">or</div>
      <button class="gbtn" data-google type="button">
        <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.1 0 24 0 14.6 0 6.4 5.4 2.5 13.3l7.8 6.1C12.2 13.2 17.6 9.5 24 9.5z"/><path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-2.8-.4-4.1H24v7.8h12.4c-.3 2.1-1.6 5.2-4.6 7.3l7.1 5.5c4.3-3.9 6.8-9.7 6.8-16.5z"/><path fill="#FBBC05" d="M10.3 28.6c-.5-1.5-.8-3-.8-4.6s.3-3.1.8-4.6l-7.8-6.1C.9 16.5 0 20.1 0 24s.9 7.5 2.5 10.7l7.8-6.1z"/><path fill="#34A853" d="M24 48c6.1 0 11.3-2 15-5.5l-7.1-5.5c-2 1.3-4.6 2.2-7.9 2.2-6.4 0-11.8-3.7-13.7-9l-7.8 6.1C6.4 42.6 14.6 48 24 48z"/></svg>
        Continue with Google
      </button>
      <div class="foothint">Save your watchlist and sync across devices.</div>
    </div>

    <div data-view="acct" style="display:none">
      <div class="acct">
        <div class="av" data-avatar>A</div>
        <h3 data-accname>Hi</h3>
        <div class="em" data-accemail></div>
        <div class="row">
          <a class="primary" href="index.html" style="text-align:center;text-decoration:none;display:block">Open the calendar →</a>
          <button class="ghost" data-signout type="button">Sign out</button>
        </div>
      </div>
    </div>
  </div>`;
document.addEventListener("DOMContentLoaded", () => document.body.appendChild(root));
if (document.body) document.body.appendChild(root);

const q = (s) => root.querySelector(s);
let mode = "signin";

function open(){ root.classList.add("on"); }
function close(){ root.classList.remove("on"); q("[data-msg]").textContent=""; }
function setMode(m){
  mode = m;
  q('[data-tab="signin"]').classList.toggle("active", m==="signin");
  q('[data-tab="signup"]').classList.toggle("active", m==="signup");
  q("[data-namewrap]").style.display = m==="signup" ? "block" : "none";
  q("[data-forgot]").style.display = m==="signin" ? "inline-block" : "none";
  q("[data-submit]").textContent = m==="signup" ? "Create account" : "Sign in";
  q("[data-title]").textContent = m==="signup" ? "Create your account" : "Welcome back";
  q("[data-password]").setAttribute("autocomplete", m==="signup" ? "new-password" : "current-password");
  q("[data-msg]").textContent = "";
}
function showError(e){
  const map = {
    "auth/invalid-credential":"Wrong email or password.",
    "auth/invalid-email":"That email doesn’t look right.",
    "auth/email-already-in-use":"An account with this email already exists — try signing in.",
    "auth/weak-password":"Password should be at least 6 characters.",
    "auth/popup-closed-by-user":"Google sign-in was cancelled.",
    "auth/operation-not-allowed":"This sign-in method isn’t enabled yet in Firebase."
  };
  const m = q("[data-msg]"); m.className = "msg err";
  m.textContent = map[e?.code] || (e?.message || "Something went wrong.").replace("Firebase: ","");
}

q("[data-close]").onclick = close;
root.addEventListener("click", (e) => { if (e.target === root) close(); });
q('[data-tab="signin"]').onclick = () => setMode("signin");
q('[data-tab="signup"]').onclick = () => setMode("signup");

q("[data-form]").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = q("[data-email]").value.trim();
  const pw = q("[data-password]").value;
  q("[data-submit]").disabled = true; q("[data-msg]").textContent = "";
  try {
    if (mode === "signup") {
      const cred = await createUserWithEmailAndPassword(auth, email, pw);
      const nm = q("[data-name]").value.trim();
      if (nm) await updateProfile(cred.user, { displayName: nm });
    } else {
      await signInWithEmailAndPassword(auth, email, pw);
    }
  } catch (err) { showError(err); }
  finally { q("[data-submit]").disabled = false; }
});

q("[data-google]").onclick = async () => {
  try { await signInWithPopup(auth, new GoogleAuthProvider()); }
  catch (err) { showError(err); }
};

q("[data-forgot]").onclick = async () => {
  const email = q("[data-email]").value.trim();
  const m = q("[data-msg]");
  if (!email) { m.className = "msg err"; m.textContent = "Enter your email first, then tap reset."; return; }
  try { await sendPasswordResetEmail(auth, email); m.className = "msg ok"; m.textContent = "Password reset email sent."; }
  catch (err) { showError(err); }
};

q("[data-signout]").onclick = () => signOut(auth);

/* auth state → swap modal view + update the nav trigger label */
onAuthStateChanged(auth, (user) => {
  const trigger = document.getElementById("authTrigger");
  // Hide "Create account" / sign-in CTAs across the page when signed in.
  document.querySelectorAll("[data-auth-open]").forEach((el) => { el.style.display = user ? "none" : ""; });
  if (user) {
    const nm = user.displayName || user.email.split("@")[0];
    q('[data-view="form"]').style.display = "none";
    q('[data-view="acct"]').style.display = "block";
    q("[data-accname]").textContent = "Hi, " + nm;
    q("[data-accemail]").textContent = user.email;
    q("[data-avatar]").textContent = (nm[0] || "A").toUpperCase();
    if (trigger) trigger.textContent = nm;
  } else {
    q('[data-view="form"]').style.display = "block";
    q('[data-view="acct"]').style.display = "none";
    setMode("signin");
    if (trigger) trigger.textContent = "Sign in";
  }
});

/* wire triggers */
function bindOpeners(){
  const t = document.getElementById("authTrigger");
  if (t) t.addEventListener("click", (e) => { e.preventDefault(); open(); });
  document.querySelectorAll("[data-auth-open]").forEach((el) =>
    el.addEventListener("click", (e) => { e.preventDefault(); setMode(el.getAttribute("data-auth-open") === "signup" ? "signup" : "signin"); open(); })
  );
}
if (document.readyState !== "loading") bindOpeners();
else document.addEventListener("DOMContentLoaded", bindOpeners);
window.openAuth = (m) => { if (m) setMode(m); open(); };

// Auto-open the sign-in / create-account popup ~5s after landing on the calendar
// home page — only when signed out, and once per browser session so it isn't nagging.
(function autoPrompt(){
  const isHome = location.pathname.endsWith("/") || /\/index\.html$/.test(location.pathname);
  if (!isHome) return;
  try { if (sessionStorage.getItem("er_auth_prompted")) return; } catch (e) {}
  setTimeout(() => {
    if (root.classList.contains("on")) return;   // already open
    if (auth.currentUser) return;                 // already signed in
    try { sessionStorage.setItem("er_auth_prompted", "1"); } catch (e) {}
    setMode("signin");
    open();
  }, 5000);
})();
