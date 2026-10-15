"""Login único no Microsoft 365 (device code). Rode: python -m assistente.login"""
import asyncio

from .config import Config
from .graph import Autenticador, Graph


async def _testar(graph: Graph) -> None:
    eu = await graph.eu()
    print(f"\n✅ Conectado como {eu.get('displayName')} <{eu.get('mail') or eu.get('userPrincipalName')}>")
    await graph.fechar()


def main() -> None:
    cfg = Config()
    auth = Autenticador(cfg.ms_client_id, cfg.ms_tenant_id, cfg.arquivo_token)
    auth.login_interativo()
    asyncio.run(_testar(Graph(auth, cfg.fuso.key)))
    print(f"Token salvo em {cfg.arquivo_token}. Já pode iniciar o bot.")


if __name__ == "__main__":
    main()
