/** A bounded FIFO. Each deadline starts at enqueue, including time waiting. */
export class PdfQueue {
  private active = false;
  private readonly waiting: (() => void)[] = [];

  run<T>(
    job: (signal: AbortSignal) => Promise<T>,
    timeout: number,
    external?: AbortSignal,
  ): Promise<T> {
    if (this.active && this.waiting.length >= 8)
      return Promise.reject(new Error('PDF queue is full (8 waiting); retry shortly'));
    return new Promise<T>((resolve, reject) => {
      const controller = new AbortController();
      let started = false;
      const abort = () => {
        controller.abort();
        if (!started) {
          const index = this.waiting.indexOf(start);
          if (index >= 0) this.waiting.splice(index, 1);
          cleanup();
          reject(new Error('PDF request cancelled or timed out while queued'));
        }
      };
      const timer = setTimeout(abort, timeout);
      const cleanup = () => {
        clearTimeout(timer);
        external?.removeEventListener('abort', abort);
      };
      const start = () => {
        started = true;
        this.active = true;
        job(controller.signal)
          .then(resolve, reject)
          .finally(() => {
            cleanup();
            this.active = false;
            this.waiting.shift()?.();
          });
      };
      external?.addEventListener('abort', abort, { once: true });
      if (external?.aborted) abort();
      else if (this.active) this.waiting.push(start);
      else start();
    });
  }
}
export const pageRenderQueue = new PdfQueue();
