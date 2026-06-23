import './style.css'
import App from './App.svelte'

const target = document.getElementById('app')

if (target === null) {
  throw new Error('App mount target is required')
}

new App({
  target,
})
