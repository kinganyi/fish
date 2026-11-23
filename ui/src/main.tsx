import { createRoot } from 'react-dom/client'
import App from './App'
import '../tailwind.css'
import './styles/tokens.css'

const root = document.getElementById('root')
if (!root) {
  throw new Error('index.html 里没有 #root 容器')
}

createRoot(root).render(<App />)
