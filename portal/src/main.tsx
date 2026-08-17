import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
// The one stylesheet (ADR-012). Imported here rather than from a component
// so Vite emits it as a linked CSS asset in the built bundle and the
// component tests stay pure markup.
import './styles.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('root container missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
