import './style.css'

const target = document.getElementById('app')

if (target === null) {
  throw new Error('App mount target is required')
}

const mount = async (): Promise<void> => {
  const Root = window.location.pathname === '/voice/livekit'
    ? (await import('./livekit/LiveKitPage.svelte')).default
    : (await import('./App.svelte')).default

  new Root({ target })
}

void mount()
