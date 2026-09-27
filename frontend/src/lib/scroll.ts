/**
 * 滚动定位：把目标元素滚到其滚动容器的**垂直居中**位置。
 *
 * 背景：工作台的双向锚定此前用 `scrollIntoView` 的 `block: "start" / "nearest"`。
 * 前者把目标贴到容器顶端、后者只在"看不见"时才动且停在最近边缘——
 * 用户落地后还要自己找目标在哪。长条款/长卡片贴顶时只能看到开头。
 *
 * 这里统一改成"居中"，并处理一个边界：**目标高于容器时不能居中**，
 * 否则连开头都会被切掉；此时退化为顶部对齐并保留 `margin` 边距。
 *
 * ⚠️ 为什么不用原生 `scrollIntoView({ block: "center" })`：它对"目标高于容器"
 * 不做退化，仍会把超高目标居中，导致条款开头看不见。此处显式比较高度后分流。
 *
 * 坐标系：用 `getBoundingClientRect()`（视口口径）做差，**不依赖 `offsetParent`**——
 * 滚动容器若未设 `position`，用 `offsetTop` 会算到 body 上，定位就错了。
 */

/** 向上寻找最近的可滚动祖先（纵向可滚且内容确实溢出）。 */
function findScrollableAncestor(el: HTMLElement): HTMLElement | null {
  let parent = el.parentElement;
  while (parent) {
    const overflowY = getComputedStyle(parent).overflowY;
    if (
      (overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay") &&
      parent.scrollHeight > parent.clientHeight
    ) {
      return parent;
    }
    parent = parent.parentElement;
  }
  return null;
}

/**
 * 把元素滚到滚动容器垂直居中。
 *
 * @param el 目标元素（正文里的高亮框、右栏的风险卡片等）
 * @param margin 目标高于容器时保留的上边距（px）
 */
export function scrollElementToCenter(el: HTMLElement, margin = 12): void {
  const container = findScrollableAncestor(el);
  if (!container) {
    // 没有可滚动祖先（如内容未溢出）：交给原生兜底
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const cRect = container.getBoundingClientRect();
  const eRect = el.getBoundingClientRect();
  const delta =
    eRect.height > cRect.height - margin * 2
      ? // 目标高于容器：顶部对齐，保证能看到开头
        eRect.top - cRect.top - margin
      : // 目标可完整展示：垂直居中
        eRect.top + eRect.height / 2 - (cRect.top + cRect.height / 2);
  container.scrollTo({ top: container.scrollTop + delta, behavior: "smooth" });
}
