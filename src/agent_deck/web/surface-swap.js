/*
 * 硬件表面按键拖拽交换控制器。
 * 边界：只处理指针手势、容器边界、拖拽视觉状态和目标命中；不理解设备型号、按键数据或保存副作用。
 */

(() => {
  "use strict";

  /** 将数值限制在闭区间内，供拖拽影子保持在硬件按键容器中。 */
  function clamp(value, minimum, maximum) {
    return Math.min(Math.max(value, minimum), maximum);
  }

  /** 判断视口坐标是否仍位于指定容器矩形内。 */
  function pointInsideRect(x, y, rect) {
    return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  }

  /** 从事件目标向上寻找属于当前硬件表面的按键元素。 */
  function closestSurfaceItem(container, target, itemSelector) {
    if (!(target instanceof Element)) return null;
    const item = target.closest(itemSelector);
    return item && container.contains(item) ? item : null;
  }

  /**
   * 管理一个硬件按键容器内的指针交换手势。
   * 控制器只维护瞬时 DOM 状态；业务数据交换和失败提示始终由注入回调负责。
   */
  class BoundedSurfaceSwapController {
    /**
     * 创建一个受容器约束的交换控制器。
     * 入参通过 options 注入容器、按键选择器、标识读取、可拖判断、可放判断和结果回调。
     * 回调只在完整拖拽结束后触发；构造过程只注册 DOM 事件，不改业务数据。
     */
    constructor(options) {
      if (!(options?.container instanceof HTMLElement)) {
        throw new TypeError("surface swap container must be an HTMLElement");
      }
      if (!options.itemSelector || typeof options.getItemId !== "function" || typeof options.onSwap !== "function") {
        throw new TypeError("surface swap requires itemSelector, getItemId and onSwap");
      }

      this.container = options.container;
      this.itemSelector = options.itemSelector;
      this.getItemId = options.getItemId;
      this.isDraggable = options.isDraggable || (() => true);
      this.canDrop = options.canDrop || (() => true);
      this.onSwap = options.onSwap;
      this.onDropRejected = options.onDropRejected || (() => {});
      this.dragThreshold = Number(options.dragThreshold ?? 6);
      this.drag = null;
      this.suppressClickUntil = 0;
      this.destroyed = false;

      this.handlePointerDown = this.handlePointerDown.bind(this);
      this.handlePointerMove = this.handlePointerMove.bind(this);
      this.handlePointerUp = this.handlePointerUp.bind(this);
      this.handlePointerCancel = this.handlePointerCancel.bind(this);
      this.handleNativeDragStart = this.handleNativeDragStart.bind(this);
      this.handleClickCapture = this.handleClickCapture.bind(this);
      this.handleKeyDown = this.handleKeyDown.bind(this);
      this.handleWindowBlur = this.handleWindowBlur.bind(this);

      this.container.addEventListener("pointerdown", this.handlePointerDown);
      this.container.addEventListener("dragstart", this.handleNativeDragStart, true);
      this.container.addEventListener("click", this.handleClickCapture, true);
      document.addEventListener("keydown", this.handleKeyDown, true);
      window.addEventListener("pointermove", this.handlePointerMove, { passive: false });
      window.addEventListener("pointerup", this.handlePointerUp);
      window.addEventListener("pointercancel", this.handlePointerCancel);
      window.addEventListener("blur", this.handleWindowBlur);
    }

    /** 返回当前是否已有按下手势占用表面，供宿主暂停会替换按键 DOM 的刷新。 */
    isDragging() {
      return this.drag !== null;
    }

    /** 记录一个可能成为拖拽的主指针按下，不影响普通点击选键。 */
    handlePointerDown(event) {
      if (this.destroyed || this.drag || event.isPrimary === false) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      const source = closestSurfaceItem(this.container, event.target, this.itemSelector);
      if (!source || !this.isDraggable(source, event)) return;

      const sourceRect = source.getBoundingClientRect();
      this.drag = {
        pointerId: event.pointerId,
        source,
        sourceId: this.getItemId(source),
        sourceRect,
        bounds: this.container.getBoundingClientRect(),
        startX: event.clientX,
        startY: event.clientY,
        offsetX: event.clientX - sourceRect.left,
        offsetY: event.clientY - sourceRect.top,
        active: false,
        ghost: null,
        target: null,
        outsideBoundary: false,
      };

      try {
        source.setPointerCapture(event.pointerId);
      } catch (_error) {
        // 浏览器不允许捕获时由 window 级 move/up/cancel 监听继续维护和清理手势。
      }
    }

    /** 在越过阈值后启动拖拽，并持续更新受边界约束的影子和放置目标。 */
    handlePointerMove(event) {
      const drag = this.drag;
      if (!drag || drag.pointerId !== event.pointerId) return;
      const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY);
      if (!drag.active && distance < this.dragThreshold) return;
      if (!drag.active) this.startDrag();
      event.preventDefault();
      this.updateDrag(event.clientX, event.clientY);
    }

    /** 创建拖拽影子并标记源键和硬件按键区域，不触发业务交换。 */
    startDrag() {
      const drag = this.drag;
      if (!drag || drag.active) return;
      drag.active = true;
      drag.ghost = drag.source.cloneNode(true);
      drag.ghost.classList.add("surface-swap-ghost");
      drag.ghost.setAttribute("aria-hidden", "true");
      drag.ghost.tabIndex = -1;
      drag.ghost.disabled = true;
      drag.ghost.style.width = `${drag.sourceRect.width}px`;
      drag.ghost.style.height = `${drag.sourceRect.height}px`;
      drag.source.classList.add("surface-swap-source");
      drag.source.setAttribute("aria-grabbed", "true");
      this.container.classList.add("surface-swap-active");
      document.body.append(drag.ghost);
    }

    /** 更新影子位置、容器边界反馈和当前可交换目标。 */
    updateDrag(clientX, clientY) {
      const drag = this.drag;
      if (!drag?.active || !drag.ghost) return;
      const maximumLeft = Math.max(drag.bounds.left, drag.bounds.right - drag.sourceRect.width);
      const maximumTop = Math.max(drag.bounds.top, drag.bounds.bottom - drag.sourceRect.height);
      const left = clamp(clientX - drag.offsetX, drag.bounds.left, maximumLeft);
      const top = clamp(clientY - drag.offsetY, drag.bounds.top, maximumTop);
      drag.ghost.style.left = `${left}px`;
      drag.ghost.style.top = `${top}px`;

      drag.outsideBoundary = !pointInsideRect(clientX, clientY, drag.bounds);
      this.container.classList.toggle("surface-swap-boundary", drag.outsideBoundary);
      drag.ghost.classList.toggle("surface-swap-boundary", drag.outsideBoundary);

      let target = null;
      if (!drag.outsideBoundary) {
        const hit = document.elementFromPoint(clientX, clientY);
        const candidate = closestSurfaceItem(this.container, hit, this.itemSelector);
        if (candidate && candidate !== drag.source && this.canDrop(drag.source, candidate)) {
          target = candidate;
        }
      }
      this.setTarget(target);
    }

    /** 切换当前放置目标的视觉和可访问状态，保证任一时刻最多高亮一个键。 */
    setTarget(target) {
      const drag = this.drag;
      if (!drag || drag.target === target) return;
      if (drag.target) {
        drag.target.classList.remove("surface-swap-target");
        drag.target.removeAttribute("data-swap-drop-target");
      }
      drag.target = target;
      if (drag.target) {
        drag.target.classList.add("surface-swap-target");
        drag.target.setAttribute("data-swap-drop-target", "true");
      }
    }

    /** 完成指针手势；有效目标触发交换，无效落点交给拒绝回调解释。 */
    handlePointerUp(event) {
      const drag = this.drag;
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (!drag.active) {
        this.finishDrag();
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      const payload = {
        sourceId: drag.sourceId,
        targetId: drag.target ? this.getItemId(drag.target) : null,
        reason: drag.target ? "swap" : drag.outsideBoundary ? "outside-boundary" : "no-target",
      };
      this.suppressClickUntil = performance.now() + 500;
      this.finishDrag();
      window.setTimeout(() => {
        if (this.destroyed) return;
        try {
          if (payload.targetId !== null) this.onSwap(payload);
          else this.onDropRejected(payload);
        } finally {
          this.suppressClickUntil = 0;
        }
      }, 0);
    }

    /** 在系统取消当前指针时清理拖拽状态，不交换任何配置。 */
    handlePointerCancel(event) {
      if (!this.drag || this.drag.pointerId !== event.pointerId) return;
      const wasActive = this.drag.active;
      const sourceId = this.drag.sourceId;
      this.finishDrag();
      if (wasActive) this.onDropRejected({ sourceId, targetId: null, reason: "cancelled" });
    }

    /**
     * 阻止当前受控手势被按键内部图片等元素升级成浏览器原生拖拽。
     * 入参：`event` 是容器捕获到的可取消 `dragstart`；只有其按键与当前指针源一致时处理。
     * 返回：无；无法解析按键或不属于当前手势时保持事件原状。
     * 错误处理：不抛错，非预期目标按无关浏览器拖拽处理。
     * 副作用：对受控事件调用 `preventDefault()`，从而保留 Pointer Events 手势序列。
     */
    handleNativeDragStart(event) {
      const source = closestSurfaceItem(this.container, event.target, this.itemSelector);
      if (!source || this.drag?.source !== source) return;
      event.preventDefault();
    }

    /** 阻止拖拽结束后浏览器合成的 click 再次选中旧源键。 */
    handleClickCapture(event) {
      if (performance.now() > this.suppressClickUntil) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      this.suppressClickUntil = 0;
    }

    /** 允许用户用 Escape 取消正在进行的拖拽，并恢复所有视觉状态。 */
    handleKeyDown(event) {
      if (event.key !== "Escape" || !this.drag) return;
      event.preventDefault();
      event.stopPropagation();
      const wasActive = this.drag.active;
      const sourceId = this.drag.sourceId;
      this.finishDrag();
      if (wasActive) this.onDropRejected({ sourceId, targetId: null, reason: "cancelled" });
    }

    /** 浏览器窗口失焦时取消拖拽，避免残留影子或目标高亮。 */
    handleWindowBlur() {
      if (!this.drag) return;
      this.finishDrag();
    }

    /** 释放 pointer capture、移除影子和全部临时样式；不调用业务回调。 */
    finishDrag() {
      const drag = this.drag;
      if (!drag) return;
      this.drag = null;
      if (drag.target) {
        drag.target.classList.remove("surface-swap-target");
        drag.target.removeAttribute("data-swap-drop-target");
      }
      drag.source.classList.remove("surface-swap-source");
      drag.source.removeAttribute("aria-grabbed");
      this.container.classList.remove("surface-swap-active", "surface-swap-boundary");
      drag.ghost?.remove();
      try {
        if (drag.source.hasPointerCapture(drag.pointerId)) {
          drag.source.releasePointerCapture(drag.pointerId);
        }
      } catch (_error) {
        // 指针已被浏览器回收时无需再次释放。
      }
    }

    /** 注销控制器及其全局监听，供未来设备 profile 卸载或页面重建时释放资源。 */
    destroy() {
      if (this.destroyed) return;
      this.finishDrag();
      this.destroyed = true;
      this.container.removeEventListener("pointerdown", this.handlePointerDown);
      this.container.removeEventListener("dragstart", this.handleNativeDragStart, true);
      this.container.removeEventListener("click", this.handleClickCapture, true);
      document.removeEventListener("keydown", this.handleKeyDown, true);
      window.removeEventListener("pointermove", this.handlePointerMove);
      window.removeEventListener("pointerup", this.handlePointerUp);
      window.removeEventListener("pointercancel", this.handlePointerCancel);
      window.removeEventListener("blur", this.handleWindowBlur);
    }
  }

  /** 创建并返回通用受限交换控制器，作为其他硬件控制台页面的复用入口。 */
  function createBoundedSwapController(options) {
    return new BoundedSurfaceSwapController(options);
  }

  window.AgentDeckSurfaceSwap = Object.freeze({ createBoundedSwapController });
})();
