const $ = (selector) => document.querySelector(selector);
const state = { credentials: null, user: null, products: [], orders: [], shipping: [], mode: "login", selected: null };
const errors = {
  "Invalid username or password": "用户名或密码错误",
  "Username already exists": "用户名已存在",
  "Product not found": "商品不存在",
  "Product unavailable or insufficient stock": "商品已下架或库存不足",
  "Stock cannot be negative": "库存不能小于零",
  "Order not found": "订单不存在，或这不是你的订单",
  "Cannot pay PAID order": "订单已经付款",
  "Cannot pay CANCELLED order": "已取消的订单不能付款",
  "Cannot cancel PAID order": "已付款订单不能取消",
  "Cannot cancel CANCELLED order": "订单已经取消",
  "Shipping task not found": "发货任务不存在，或这不是你的订单",
  "Only failed shipping tasks can be retried": "只能重试发货失败或等待重试的任务，请刷新后查看最新状态",
};

function message(text, isError = false) {
  const toast = $("#toast");
  toast.textContent = text;
  toast.classList.toggle("error", isError);
  toast.classList.remove("hidden");
  clearTimeout(message.timer);
  message.timer = setTimeout(() => toast.classList.add("hidden"), 4000);
}

function basicHeader() {
  const bytes = new TextEncoder().encode(`${state.credentials.username}:${state.credentials.password}`);
  return `Basic ${btoa(String.fromCharCode(...bytes))}`;
}

async function request(path, options = {}) {
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}) };
  if (state.credentials) headers.Authorization = basicHeader();
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch {
    throw new Error("无法连接服务器，请检查服务是否正在运行");
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) throw new Error("登录信息已失效，请重新登录");
    const detail = data?.detail;
    const text = typeof detail === "string" ? (errors[detail] || detail) : "输入内容不符合要求，请检查后重试";
    throw new Error(text);
  }
  return data;
}

async function action(work, success) {
  try {
    await work();
    if (success) message(success);
  } catch (error) {
    message(error.message, true);
  }
}

function money(cents) { return `¥${(cents / 100).toFixed(2)}`; }
function cents(value) {
  if (!/^\d+(\.\d{1,2})?$/.test(value)) throw new Error("价格最多保留两位小数");
  return Math.round(Number(value) * 100);
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(label, className, handler, disabled = false) {
  const node = element("button", `button ${className}`, label);
  node.type = "button";
  node.disabled = disabled;
  node.addEventListener("click", handler);
  return node;
}

function showLoggedIn() {
  $("#auth-view").classList.add("hidden");
  $("#dashboard").classList.remove("hidden");
  $("#logout-button").classList.remove("hidden");
  $("#current-user").textContent = `当前用户：${state.user.username}`;
}
function logout() {
  state.credentials = null;
  state.user = null;
  state.products = [];
  state.orders = [];
  state.shipping = [];
  $("#dashboard").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
  $("#logout-button").classList.add("hidden");
  $("#current-user").textContent = "尚未登录";
  $("#password").value = "";
  message("已退出登录");
}
function setAuthMode(mode) {
  state.mode = mode;
  const register = mode === "register";
  $("#login-tab").classList.toggle("active", !register);
  $("#register-tab").classList.toggle("active", register);
  $("#login-tab").setAttribute("aria-selected", String(!register));
  $("#register-tab").setAttribute("aria-selected", String(register));
  $("#auth-title").textContent = register ? "创建账号" : "欢迎回来";
  $("#auth-hint").textContent = register ? "注册后即可管理商品和创建订单。" : "输入账号和密码，继续体验平台。";
  $("#auth-submit").textContent = register ? "注册并进入" : "登录";
  $("#password").autocomplete = register ? "new-password" : "current-password";
}

async function refreshProducts() {
  state.products = await request("/products");
  renderProducts();
  renderStats();
}
async function refreshOrders() {
  state.orders = await request("/orders");
  renderOrders();
  renderStats();
}
async function refreshShipping() {
  const credentials = state.credentials;
  const tasks = await request("/shipping-tasks");
  if (state.credentials !== credentials) return;
  state.shipping = tasks;
  renderShipping();
  renderStats();
}
async function refreshAll() { await Promise.all([refreshProducts(), refreshOrders(), refreshShipping()]); }
function renderStats() {
  $("#product-count").textContent = state.products.length;
  $("#order-count").textContent = state.orders.length;
  $("#pending-count").textContent = state.orders.filter((order) => order.status === "PENDING_PAYMENT").length;
  $("#failed-count").textContent = state.shipping.filter((task) => task.status === "FAILED").length;
}

function renderProducts() {
  const list = $("#product-list");
  list.replaceChildren();
  if (!state.products.length) {
    list.append(element("div", "empty", "还没有商品，先添加一个试试吧。"));
    return;
  }
  for (const product of state.products) {
    const card = element("article", "item");
    const top = element("div", "item-top");
    const left = element("div");
    left.append(element("h3", "", product.name), element("div", "id", `商品 #${product.id}`));
    top.append(left, element("span", "price", money(product.price_cents)));
    const meta = element("div", "meta");
    meta.append(element("span", "", `可用库存：${product.available_stock}`), element("span", `pill ${product.is_active ? "" : "off"}`, product.is_active ? "已上架" : "已下架"));
    const actions = element("div", "item-actions");
    actions.append(
      button("创建订单", "primary", () => openOrder(product), !product.is_active || product.available_stock === 0),
      button("修改信息", "subtle", () => openEdit(product)),
      button("调整库存", "subtle", () => openStock(product)),
      button(product.is_active ? "下架" : "上架", "ghost", () => action(async () => {
        await request(`/products/${product.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !product.is_active }) });
        await refreshProducts();
      }, product.is_active ? "商品已下架" : "商品已上架")),
    );
    card.append(top, meta, actions);
    list.append(card);
  }
}

const statusLabels = { PENDING_PAYMENT: "待付款", PAID: "已付款", CANCELLED: "已取消" };
const statusClasses = { PENDING_PAYMENT: "pending", PAID: "paid", CANCELLED: "cancelled" };
function renderOrders() {
  const list = $("#order-list");
  list.replaceChildren();
  if (!state.orders.length) {
    list.append(element("div", "empty", "还没有订单，从左侧商品列表创建一个吧。"));
    return;
  }
  for (const order of state.orders) {
    const card = element("article", "item");
    const top = element("div", "item-top");
    const left = element("div");
    left.append(element("h3", "", order.product_name), element("div", "id", `订单 #${order.id}`));
    top.append(left, element("span", "price", money(order.total_cents)));
    const meta = element("div", "meta");
    meta.append(element("span", "", `数量：${order.quantity}`), element("span", "", `单价：${money(order.unit_price_cents)}`), element("span", `pill ${statusClasses[order.status]}`, statusLabels[order.status] || order.status));
    const actions = element("div", "item-actions");
    if (order.status === "PENDING_PAYMENT") {
      actions.append(
        button("模拟付款", "primary", () => action(async () => {
          await request(`/orders/${order.id}/pay`, { method: "POST" });
          await Promise.all([refreshOrders(), refreshShipping()]);
        }, "模拟付款成功，已安排后台发货")),
        button("取消订单", "ghost", () => action(async () => {
          await request(`/orders/${order.id}/cancel`, { method: "POST" });
          await refreshAll();
        }, "订单已取消，库存已归还")),
      );
    }
    actions.append(button("删除订单", "danger", () => {
      const prompt = order.status === "PENDING_PAYMENT" ? "删除待付款订单会取消订单并归还库存。确定删除吗？" : "订单将从你的列表隐藏。确定删除吗？";
      if (!window.confirm(prompt)) return;
      action(async () => {
        await request(`/orders/${order.id}`, { method: "DELETE" });
        await refreshAll();
      }, "订单已删除");
    }));
    card.append(top, meta, actions);
    list.append(card);
  }
}

const shippingLabels = { PENDING: "待发货", SUCCEEDED: "模拟已发货", FAILED: "发货失败" };
const shippingClasses = { PENDING: "pending", SUCCEEDED: "paid", FAILED: "failed" };
function renderShipping() {
  const list = $("#shipping-list");
  list.replaceChildren();
  if (!state.shipping.length) {
    list.append(element("div", "empty", "付款成功后，发货任务会出现在这里。"));
    return;
  }
  for (const task of state.shipping) {
    const card = element("article", `item ${task.status === "FAILED" ? "needs-attention" : ""}`);
    const top = element("div", "item-top");
    const left = element("div");
    left.append(element("h3", "", `订单 #${task.order_id}`));
    const label = task.status === "PENDING" && task.last_error ? "等待自动重试" : shippingLabels[task.status];
    top.append(left, element("span", `pill ${shippingClasses[task.status]}`, label));
    card.append(top);
    if (task.tracking_no) {
      const meta = element("div", "meta");
      meta.append(element("span", "", `模拟运单：${task.tracking_no}`));
      card.append(meta);
    }
    if (task.last_error) card.append(element("p", "shipping-error", `失败原因：${task.last_error}`));
    if (task.status === "FAILED" || (task.status === "PENDING" && task.last_error)) {
      const actions = element("div", "item-actions");
      actions.append(button("重新发货", "primary", () => action(async () => {
        await request(`/shipping-tasks/${task.id}/retry`, { method: "POST" });
        await refreshShipping();
      }, "已重新安排后台发货")));
      card.append(actions);
    }
    list.append(card);
  }
}

function openEdit(product) {
  state.selected = product;
  $("#edit-title").textContent = `修改「${product.name}」`;
  $("#edit-name").value = product.name;
  $("#edit-price").value = (product.price_cents / 100).toFixed(2);
  $("#edit-dialog").showModal();
}
function openStock(product) {
  state.selected = product;
  $("#stock-title").textContent = `调整「${product.name}」库存`;
  $("#stock-delta").value = "";
  $("#stock-dialog").showModal();
}
function openOrder(product) {
  state.selected = product;
  $("#order-title").textContent = `购买「${product.name}」`;
  $("#order-summary").textContent = `单价 ${money(product.price_cents)}，当前可用库存 ${product.available_stock} 件。`;
  $("#order-quantity").value = "1";
  $("#order-quantity").max = String(product.available_stock);
  $("#order-dialog").showModal();
}

$("#login-tab").addEventListener("click", () => setAuthMode("login"));
$("#register-tab").addEventListener("click", () => setAuthMode("register"));
$("#logout-button").addEventListener("click", logout);
$("#auth-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const username = $("#username").value.trim();
    const password = $("#password").value;
    if (state.mode === "register") await request("/users", { method: "POST", body: JSON.stringify({ username, password }) });
    state.credentials = { username, password };
    try {
      state.user = await request("/users/me");
      await refreshAll();
    } catch (error) {
      state.credentials = null;
      state.user = null;
      throw error;
    }
    showLoggedIn();
    $("#password").value = "";
  }, state.mode === "register" ? "注册成功" : "登录成功");
});
$("#refresh-products").addEventListener("click", () => action(refreshProducts, "商品已刷新"));
$("#refresh-orders").addEventListener("click", () => action(refreshOrders, "订单已刷新"));
$("#refresh-shipping").addEventListener("click", () => action(refreshShipping, "发货任务已刷新"));
$("#product-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    await request("/products", { method: "POST", body: JSON.stringify({
      name: $("#product-name").value.trim(),
      price_cents: cents($("#product-price").value),
      available_stock: Number($("#product-stock").value),
    }) });
    $("#product-form").reset();
    await refreshProducts();
  }, "商品已添加");
});
$("#edit-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    await request(`/products/${state.selected.id}`, { method: "PATCH", body: JSON.stringify({
      name: $("#edit-name").value.trim(), price_cents: cents($("#edit-price").value),
    }) });
    $("#edit-dialog").close();
    await refreshProducts();
  }, "商品信息已更新");
});
$("#stock-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const delta = Number($("#stock-delta").value);
    if (!Number.isInteger(delta) || delta === 0) throw new Error("请输入非零整数");
    await request(`/products/${state.selected.id}/stock-adjustments`, { method: "POST", body: JSON.stringify({ delta }) });
    $("#stock-dialog").close();
    await refreshProducts();
  }, "库存已调整");
});
$("#order-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    await request("/orders", { method: "POST", body: JSON.stringify({
      product_id: state.selected.id, quantity: Number($("#order-quantity").value),
    }) });
    $("#order-dialog").close();
    await refreshAll();
  }, "订单已创建，库存已占用");
});
for (const [buttonId, dialogId] of [["#edit-cancel", "#edit-dialog"], ["#stock-cancel", "#stock-dialog"], ["#order-cancel", "#order-dialog"]]) {
  $(buttonId).addEventListener("click", () => $(dialogId).close());
}
setInterval(() => {
  if (state.user && !document.hidden) refreshShipping().catch(() => {});
}, 3000);
