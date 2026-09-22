export type HttpErrorMapper = (response: Response) => Promise<Error | null>

export const ensureOk = (response: Response, label: string): Response => {
  if (!response.ok) throw new Error(`${label} failed with status ${response.status}`)
  return response
}

export const requestJson = async (
  url: string,
  label: string,
  init?: RequestInit,
  mapError?: HttpErrorMapper,
): Promise<unknown> => {
  const response = await fetch(url, init)
  if (!response.ok) {
    const mapped = mapError === undefined ? null : await mapError(response)
    throw mapped ?? new Error(`${label} failed with status ${response.status}`)
  }
  return response.status === 204 ? null : response.json()
}

export const requestVoid = async (
  url: string,
  init: RequestInit,
  label: string,
): Promise<void> => {
  ensureOk(await fetch(url, init), label)
}
