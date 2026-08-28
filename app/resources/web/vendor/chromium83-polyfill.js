/*!
 * Chromium 83 (Qt 5.15.2 WebEngine) 缺失 API 的最小 polyfill。
 *
 * 背景：Qt 5.15.2 的 QtWebEngine 基于 Chromium 83.0.4103.122（实测
 * navigator.userAgent 确认）。现代前端库（mermaid 10 等）构建时的 target
 * 普遍高于该版本，会因缺少 ES2021+ 的运行时 API 而在模块顶层就抛错，
 * 表现为整个库 undefined。本文件提供四个缺失点的等价实现。
 *
 * 设计原则：
 *   1. 全部走特性探测（if (!X)），将来升级到 Qt6 / 新版 Chromium 后
 *      本文件自动退化为 no-op，不会覆盖原生实现。
 *   2. 不引入任何新语法（无 ??、?.、??=、箭头以外的新特性），
 *      确保自身能被 Chromium 83 正确解析。
 *   3. 同步执行，必须在依赖它的 vendor 库之前以 <script> 引入。
 *
 * 覆盖的缺失点（括号内为原生所需的最低 Chrome 版本）：
 *   String.prototype.replaceAll   (85)
 *   Object.hasOwn                 (93)
 *   Array.prototype.at            (92)
 *   structuredClone               (98)
 */
(function (global) {
  'use strict';

  // ── String.prototype.replaceAll (Chrome 85) ──
  if (typeof String.prototype.replaceAll !== 'function') {
    // 全局替换时正则必须带 g 标志；字符串参数走 split/join 避免转义问题
    String.prototype.replaceAll = function (search, replacement) {
      var str = String(this);
      if (search instanceof RegExp) {
        if (!search.global) {
          throw new TypeError('replaceAll must be called with a global RegExp');
        }
        return str.replace(search, replacement);
      }
      return str.split(String(search)).join(String(replacement));
    };
  }

  // ── Object.hasOwn (Chrome 93) ──
  if (typeof Object.hasOwn !== 'function') {
    Object.hasOwn = function (obj, key) {
      if (obj == null) {
        throw new TypeError('Object.hasOwn called on null or undefined');
      }
      return Object.prototype.hasOwnProperty.call(Object(obj), key);
    };
  }

  // ── Array.prototype.at (Chrome 92) ──
  if (typeof Array.prototype.at !== 'function') {
    Array.prototype.at = function (index) {
      var len = this.length >>> 0;
      var i = Number(index) || 0;
      var k = i < 0 ? len + i : i;
      return k >= 0 && k < len ? this[k] : undefined;
    };
  }

  // ── structuredClone (Chrome 98) ──
  // 完整实现需要覆盖 Map/Set/Date/RegExp/TypedArray/循环引用等，
  // 这里做到"够用即可"：vendor 库（mermaid）主要用它克隆配置与状态对象。
  if (typeof global.structuredClone !== 'function') {
    var clone = function (value, seen) {
      // 原始值与函数直接返回
      if (value === null || typeof value !== 'object') {
        return value;
      }

      // 循环引用：返回已克隆过的引用，避免栈溢出
      var i;
      if (seen) {
        for (i = 0; i < seen.length; i++) {
          if (seen[i].src === value) {
            return seen[i].dst;
          }
        }
      } else {
        seen = [];
      }

      var out;

      if (value instanceof Date) {
        return new Date(value.getTime());
      }
      if (value instanceof RegExp) {
        return new RegExp(value.source, value.flags);
      }
      if (value instanceof Map) {
        out = new Map();
        seen.push({ src: value, dst: out });
        value.forEach(function (v, k) {
          out.set(clone(k, seen), clone(v, seen));
        });
        return out;
      }
      if (value instanceof Set) {
        out = new Set();
        seen.push({ src: value, dst: out });
        value.forEach(function (v) {
          out.add(clone(v, seen));
        });
        return out;
      }
      if (Array.isArray(value)) {
        out = [];
        seen.push({ src: value, dst: out });
        for (i = 0; i < value.length; i++) {
          out[i] = clone(value[i], seen);
        }
        return out;
      }
      if (ArrayBuffer.isView(value)) {
        // TypedArray / DataView：复制底层 buffer
        return new value.constructor(value.buffer.slice(0));
      }
      if (value instanceof ArrayBuffer) {
        return value.slice(0);
      }

      // 普通对象与自定义类实例
      out = Object.create(Object.getPrototypeOf(value));
      seen.push({ src: value, dst: out });
      var keys = Object.keys(value);
      for (i = 0; i < keys.length; i++) {
        out[keys[i]] = clone(value[keys[i]], seen);
      }
      return out;
    };

    global.structuredClone = function (value) {
      return clone(value, null);
    };
  }
})(typeof globalThis !== 'undefined' ? globalThis : window);
