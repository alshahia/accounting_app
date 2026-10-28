interface ListErrorProps {
  message: string;
  retryLabel: string;
  onRetry: () => void;
}

/** AC-24: a failed list must offer a retry, never pretend the list is empty. */
export function ListError({ message, retryLabel, onRetry }: ListErrorProps) {
  return (
    <div className="empty-state list-error" role="alert">
      <p>{message}</p>
      <button type="button" className="button ghost" onClick={onRetry}>
        {retryLabel}
      </button>
    </div>
  );
}
