import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import ThemeModeProvider from './ThemeModeProvider'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ThemeModeProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemeModeProvider>
  </React.StrictMode>,
)
