/** A bounded FIFO. Each deadline starts at enqueue, including time waiting.
 * The job's signal carries the abort reason: 'timeout' or 'cancelled'. */
export class PdfQueue {
  private active = false;
  private readonly waiting: (() => void)[] = [];

  run<T>(
    job: (signal: AbortSignal) => Promise<T>,
    timeout: number,
    external?: AbortSignal,
  ): Promise<T> {
    if (this.active && this.waiting.length >= 8)
      return Promise.reject(
        new Error(
          'The request was valid; the server is busy: PDF queue is full (8 waiting); retry shortly',
        ),
      );
    return new Promise<T>((resolve, reject) => {
      const controller = new AbortController();
      let started = false;
      const abort = (reason: 'timeout' | 'cancelled') => {
        controller.abort(reason);
        if (!started) {
          const index = this.waiting.indexOf(start);
          if (index >= 0) this.waiting.splice(index, 1);
          cleanup();
          reject(
            new Error(
              reason === 'timeout'
                ? `The request was valid; the PDF helper failed: PDF request timed out after ${timeout / 1000} s while queued behind other PDF work; retry shortly`
                : 'PDF request was cancelled while queued',
            ),
          );
        }
      };
      const cancel = () => abort('cancelled');
      const timer = setTimeout(() => abort('timeout'), timeout);
      const cleanup = () => {
        clearTimeout(timer);
        external?.removeEventListener('abort', cancel);
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
      external?.addEventListener('abort', cancel, { once: true });
      if (external?.aborted) cancel();
      else if (this.active) this.waiting.push(start);
      else start();
    });
  }
}
export const pageRenderQueue = new PdfQueue();
