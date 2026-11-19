import React, { useEffect, useMemo, useState } from "react";

const money = (c) => `$${(c / 100).toFixed(2)}`;

async function api(path, opts = {}) {
  const r = await fetch(`/api${path}`, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail ? String(body.detail) : `Request failed (${r.status})`);
  return body;
}

// Tiny hash router: #/, #/shop, #/product/3, #/cart, #/orders
function useRoute() {
  const [hash, setHash] = useState(location.hash || "#/");
  useEffect(() => {
    const on = () => { setHash(location.hash || "#/"); window.scrollTo(0, 0); };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return hash.slice(2).split("/");
}

function useCart() {
  const [cart, setCart] = useState(() => {
    try { return JSON.parse(localStorage.getItem("sf_cart") || "{}"); } catch { return {}; }
  });
  useEffect(() => { try { localStorage.setItem("sf_cart", JSON.stringify(cart)); } catch { /* private mode */ } }, [cart]);
  const add = (id, n = 1) => setCart((c) => ({ ...c, [id]: Math.min((c[id] || 0) + n, 20) }));
  const set = (id, n) => setCart((c) => { const x = { ...c }; if (n <= 0) delete x[id]; else x[id] = Math.min(n, 20); return x; });
  const clear = () => setCart({});
  const count = Object.values(cart).reduce((a, b) => a + b, 0);
  return { cart, add, set, clear, count };
}

function Notice({ error }) {
  if (!error) return null;
  return <div className="notice" role="alert">⚠️ {error} — please try again in a moment.</div>;
}

function ProductCard({ p, onAdd }) {
  return (
    <article className="card">
      <a href={`#/product/${p.id}`} className="card-img" aria-label={p.name}>{p.image}</a>
      <div className="card-body">
        <span className="tag">{p.category}</span>
        <a href={`#/product/${p.id}`} className="card-title">{p.name}</a>
        <div className="card-foot">
          <strong>{money(p.price_cents)}</strong>
          <button onClick={() => onAdd(p.id)}>Add to cart</button>
        </div>
      </div>
    </article>
  );
}

function Home({ products, onAdd }) {
  return (
    <>
      <section className="hero">
        <div>
          <p className="eyebrow">Spring collection</p>
          <h1>Everyday gear, thoughtfully made.</h1>
          <p className="lead">Audio, wearables and outdoor essentials. Free shipping on orders over $50.</p>
          <a className="btn" href="#/shop">Shop the catalog</a>
        </div>
        <div className="hero-art" aria-hidden="true">🎧⌚🎒</div>
      </section>
      <h2 className="section-title">Featured</h2>
      <div className="grid">{products.slice(0, 4).map((p) => <ProductCard key={p.id} p={p} onAdd={onAdd} />)}</div>
    </>
  );
}

function Shop({ products, onAdd }) {
  const [cat, setCat] = useState("All");
  const cats = useMemo(() => ["All", ...new Set(products.map((p) => p.category))], [products]);
  const shown = cat === "All" ? products : products.filter((p) => p.category === cat);
  return (
    <>
      <h1 className="page-title">Catalog</h1>
      <div className="filters" role="tablist">
        {cats.map((c) => <button key={c} role="tab" aria-selected={c === cat} className={c === cat ? "chip active" : "chip"} onClick={() => setCat(c)}>{c}</button>)}
      </div>
      <div className="grid">{shown.map((p) => <ProductCard key={p.id} p={p} onAdd={onAdd} />)}</div>
    </>
  );
}

function Product({ id, onAdd }) {
  const [p, setP] = useState(null);
  const [err, setErr] = useState("");
  const [qty, setQty] = useState(1);
  useEffect(() => { setP(null); setErr(""); api(`/products/${id}`).then(setP).catch((e) => setErr(e.message)); }, [id]);
  if (err) return <Notice error={err} />;
  if (!p) return <p className="muted">Loading…</p>;
  return (
    <>
      <a href="#/shop" className="back">← Back to catalog</a>
      <section className="detail">
        <div className="detail-img">{p.image}</div>
        <div>
          <span className="tag">{p.category}</span>
          <h1>{p.name}</h1>
          <p className="price">{money(p.price_cents)}</p>
          <p>{p.description}</p>
          <p className={p.stock > 10 ? "stock ok" : "stock low"}>{p.stock > 10 ? "In stock" : `Only ${p.stock} left`} · SKU {p.sku}</p>
          <div className="buy">
            <label>Qty <input type="number" min="1" max="20" value={qty} onChange={(e) => setQty(Math.max(1, +e.target.value || 1))} /></label>
            <button onClick={() => onAdd(p.id, qty)}>Add to cart</button>
          </div>
        </div>
      </section>
      {p.recommendations.length > 0 && (
        <>
          <h2 className="section-title">You may also like</h2>
          <div className="grid">{p.recommendations.map((r) => <ProductCard key={r.id} p={r} onAdd={onAdd} />)}</div>
        </>
      )}
    </>
  );
}

function Cart({ cart, set, clear }) {
  const [quote, setQuote] = useState(null);
  const [email, setEmail] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [placed, setPlaced] = useState(null);
  const items = Object.entries(cart).map(([product_id, quantity]) => ({ product_id: +product_id, quantity }));
  useEffect(() => {
    setErr("");
    if (!items.length) { setQuote(null); return; }
    api("/cart/quote", { method: "POST", body: JSON.stringify({ items }) }).then(setQuote).catch((e) => setErr(e.message));
  }, [JSON.stringify(cart)]);
  const checkout = async (e) => {
    e.preventDefault(); setBusy(true); setErr("");
    try {
      const r = await api("/checkout", { method: "POST", body: JSON.stringify({ email, items }) });
      setPlaced(r); clear();
      try { localStorage.setItem("sf_email", email); } catch { /* ignore */ }
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  };
  if (placed) return (
    <section className="confirm">
      <h1>Thank you! 🎉</h1>
      <p>Order <strong>#{placed.order_id}</strong> is {placed.status}. Total {money(placed.total_cents)}.</p>
      <a className="btn" href="#/orders">View your orders</a>
    </section>
  );
  if (!items.length) return <section className="empty"><h1 className="page-title">Your cart is empty</h1><a className="btn" href="#/shop">Start shopping</a></section>;
  return (
    <>
      <h1 className="page-title">Cart</h1>
      <Notice error={err} />
      <div className="cart">
        <table>
          <thead><tr><th>Product</th><th>Qty</th><th className="num">Total</th></tr></thead>
          <tbody>
            {(quote?.lines || []).map((l) => (
              <tr key={l.product_id}>
                <td>{l.name}<div className="muted">{money(l.unit_cents)} each</div></td>
                <td><input type="number" min="0" max="20" value={cart[l.product_id] || 0} aria-label={`Quantity of ${l.name}`}
                  onChange={(e) => set(l.product_id, +e.target.value || 0)} /></td>
                <td className="num">{money(l.unit_cents * l.quantity)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {quote && (
          <form className="summary" onSubmit={checkout}>
            <div className="row"><span>Subtotal</span><span>{money(quote.subtotal_cents)}</span></div>
            <div className="row"><span>Shipping</span><span>{quote.shipping_cents ? money(quote.shipping_cents) : "Free"}</span></div>
            <div className="row total"><span>Total</span><span>{money(quote.total_cents)}</span></div>
            <label htmlFor="email">Email for your receipt</label>
            <input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
            <button disabled={busy}>{busy ? "Placing order…" : "Place order"}</button>
          </form>
        )}
      </div>
    </>
  );
}

function Orders() {
  const [email, setEmail] = useState(() => { try { return localStorage.getItem("sf_email") || ""; } catch { return ""; } });
  const [orders, setOrders] = useState(null);
  const [err, setErr] = useState("");
  const look = (e) => { e?.preventDefault(); setErr(""); api(`/orders?email=${encodeURIComponent(email)}`).then(setOrders).catch((x) => setErr(x.message)); };
  useEffect(() => { if (email) look(); }, []);
  return (
    <>
      <h1 className="page-title">Your orders</h1>
      <form className="lookup" onSubmit={look}>
        <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" aria-label="Email" />
        <button>Find orders</button>
      </form>
      <Notice error={err} />
      {orders && (orders.length ? (
        <table className="orders"><thead><tr><th>Order</th><th>Placed</th><th>Items</th><th>Status</th><th className="num">Total</th></tr></thead>
          <tbody>{orders.map((o) => (
            <tr key={o.id}><td>#{o.id}</td><td>{new Date(o.created_at).toLocaleString()}</td>
              <td>{o.items.reduce((a, i) => a + i.quantity, 0)}</td><td><span className="tag">{o.status}</span></td><td className="num">{money(o.total_cents)}</td></tr>))}
          </tbody></table>
      ) : <p className="muted">No orders for this email yet.</p>)}
    </>
  );
}

export default function App() {
  const route = useRoute();
  const { cart, add, set, clear, count } = useCart();
  const [products, setProducts] = useState([]);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  useEffect(() => { api("/products").then(setProducts).catch((e) => setErr(e.message)); }, []);
  const onAdd = (id, n = 1) => { add(id, n); setToast("Added to cart"); setTimeout(() => setToast(""), 1400); };
  const page = route[0] || "";
  return (
    <>
      <header className="nav">
        <a href="#/" className="brand">🛍️ ShopFront</a>
        <nav>
          <a href="#/shop" className={page === "shop" ? "on" : ""}>Shop</a>
          <a href="#/orders" className={page === "orders" ? "on" : ""}>Orders</a>
          <a href="#/cart" className={page === "cart" ? "on cart-link" : "cart-link"}>Cart{count > 0 && <span className="badge">{count}</span>}</a>
        </nav>
      </header>
      <main>
        {page !== "product" && page !== "cart" && page !== "orders" && <Notice error={err} />}
        {page === "" && <Home products={products} onAdd={onAdd} />}
        {page === "shop" && <Shop products={products} onAdd={onAdd} />}
        {page === "product" && <Product id={route[1]} onAdd={onAdd} />}
        {page === "cart" && <Cart cart={cart} set={set} clear={clear} />}
        {page === "orders" && <Orders />}
      </main>
      {toast && <div className="toast" role="status">{toast}</div>}
      <footer>ShopFront demo store · monitored by AegisOps</footer>
    </>
  );
}
