"""Categorização local de produtos (sem IA).

Regras:
- O nome é normalizado (maiúsculas, sem acentos, pontuação vira espaço), então
  "REFRIG." e "Refrig" casam com a palavra-chave "REFRIG".
- Toda palavra-chave casa só como palavra inteira (regex \\b...\\b): "DET" não casa
  com "DETALHE", "SAL" não casa com "SALSICHA".
- Frases (mais de uma palavra) são testadas primeiro, da mais longa para a mais curta.
  Assim "AGUA SANITARIA" vence "AGUA" e "CREME DE LEITE" vence "LEITE".
- Se nenhuma frase casar, vale a palavra isolada que aparece primeiro no nome
  (em NFC-e o tipo do produto costuma vir no início: "SUCO DEL VALLE LARANJA").
"""

import re

from .util import normalizar_nome

OUTROS = "Outros"

TIPOS_ESTABELECIMENTO = (
    "mercado", "padaria", "farmacia", "posto", "restaurante", "casa_utilidades", "vestuario",
    "educacao", "transporte", "lazer", "servicos", "outro",
)

PALAVRAS: dict[str, list[str]] = {
    "Hortifruti": [
        "BANANA", "MACA", "LARANJA", "LIMAO", "TOMATE", "BATATA", "BATATA DOCE", "CEBOLA", "ALHO",
        "CENOURA", "ALFACE", "MAMAO", "MELANCIA", "MELAO", "UVA", "MORANGO", "ABACAXI", "MANGA",
        "PEPINO", "PIMENTAO", "ABOBRINHA", "ABOBORA", "CHUCHU", "REPOLHO", "BROCOLIS", "COUVE",
        "MANDIOCA", "INHAME", "BETERRABA", "QUIABO", "JILO", "VAGEM", "COENTRO", "CHEIRO VERDE",
        "CEBOLINHA", "SALSA", "ABACATE", "GOIABA", "MARACUJA", "KIWI", "PERA", "TANGERINA",
        "PONKAN", "BATATINHA", "HORTIFRUTI", "FLV", "OVO", "OVOS", "OVOS BRANCOS", "OVOS VERMELHOS",
    ],
    "Carnes": [
        "CARNE", "CARNE MOIDA", "BOVINA", "BOV", "FRANGO", "FGO", "PEITO FGO", "PEITO FRANGO",
        "COXA", "SOBRECOXA", "ASA", "COXINHA ASA", "LINGUICA", "LING", "SALSICHA", "BACON",
        "PATINHO", "ACEM", "ALCATRA", "PICANHA", "COSTELA", "SUINO", "SUINA", "PERNIL", "LOMBO",
        "TILAPIA", "PEIXE", "FILE", "CONTRA FILE", "MAMINHA", "CUPIM", "FRALDINHA", "MUSCULO",
        "COXAO", "COXAO MOLE", "COXAO DURO", "HAMBURGUER", "ALMONDEGA", "CALABRESA", "TOSCANA",
        "MIUDOS", "FIGADO", "SARDINHA FRESCA",
    ],
    "Frios e Laticínios": [
        "LEITE", "LTE", "LT INTEGRAL", "LT DESN", "LT UHT", "LEITE UHT", "QUEIJO", "QJO",
        "MUSSARELA", "MUCARELA", "MUSS", "PRATO", "QJO PRATO", "PRESUNTO", "APRESUNTADO", "APRES",
        "MORTADELA", "MORT", "SALAME", "PEITO PERU", "PEITO DE PERU", "IOGURTE", "IOG", "REQUEIJAO",
        "REQ", "MANTEIGA", "MANT", "MARGARINA", "MARG", "NATA", "COALHADA", "BEBIDA LACTEA",
        "LEITE FERM", "LEITE FERMENTADO", "BEB LACTEA", "BEB LACT", "PETIT SUISSE", "CREAM CHEESE", "RICOTA", "PARMESAO",
    ],
    "Padaria": [
        "PAO", "PAES", "PAO FORMA", "PAO FRANCES", "PAO DE QUEIJO", "PAO QUEIJO", "BISNAGUINHA",
        "ROSCA", "BOLO", "TORRADA", "SONHO", "BROA", "CROISSANT", "PANETONE",
    ],
    "Mercearia": [
        "ARROZ", "FEIJAO", "FEIJ", "MACARRAO", "MAC INSTANT", "ESPAGUETE", "PENNE", "PARAFUSO",
        "ACUCAR", "ACUC", "ADOCANTE", "OLEO", "OLEO SOJA", "AZEITE", "SAL", "SAL REFINADO", "CAFE",
        "FARINHA", "FAR TRIGO", "FAR MANDIOCA", "FUBA", "AVEIA", "BISC", "BISCOITO", "BOLACHA",
        "WAFER", "MOLHO", "MOLHO TOMATE", "EXTRATO", "EXTRATO TOMATE", "MAIONESE", "KETCHUP",
        "CATCHUP", "MOSTARDA", "TEMPERO", "VINAGRE", "MILHO", "MILHO VERDE", "ERVILHA", "SARDINHA",
        "ATUM", "ACHOC", "ACHOCOLATADO", "LEITE CONDENSADO", "LEITE COND", "LT COND", "CREME DE LEITE",
        "CR LEITE", "CREME LEITE", "LEITE DE COCO", "LEITE COCO", "LEITE EM PO", "LEITE PO", "LT PO",
        "GELATINA", "CHOCOLATE", "CHOC", "BOMBOM", "MIOJO", "LAMEN", "MASSA", "GRANOLA", "CEREAL",
        "PIPOCA", "AMENDOIM", "BATATA PALHA", "BATATA CHIPS", "SALGADINHO", "DOCE", "GELEIA", "MEL",
        "COLORAU", "CALDO", "SOPA", "AZEITONA", "PALMITO", "CANJICA", "TAPIOCA", "GOMA", "POLVILHO",
        "FERMENTO", "CHA", "CHA MATE", "BALA", "CHICLETE", "PAPINHA", "MISTURA BOLO",
        "MIST BOLO", "NUTELLA", "CREME DE AVELA", "MISTURA P BOLO", "MISTURA PARA BOLO", "SHOYU", "AJINOSHOYU",
    ],
    "Bebidas": [
        "REFRIG", "REFRIGERANTE", "REFRI", "BEB", "BEBIDA", "COCA", "COCA COLA", "PEPSI", "GUARANA", "FANTA", "SPRITE",
        "SUCO", "NECTAR", "AGUA", "AGUA MIN", "AGUA MINERAL", "AGUA DE COCO", "AGUA COCO", "ENERG",
        "ENERGETICO", "ISOTONICO", "TONICA", "H2OH", "CHA GELADO", "CHA PRONTO",
    ],
    "Bebidas Alcoólicas": [
        "CERV", "CERVEJA", "VINHO", "VODKA", "WHISKY", "CACHACA", "GIN", "RUM", "LICOR", "ESPUMANTE",
        "SKOL", "BRAHMA", "HEINEKEN", "AMSTEL", "BUDWEISER", "SPATEN", "STELLA", "ITAIPAVA",
        "ANTARCTICA", "CHOPP", "CHOPE", "SIDRA", "BEATS",
    ],
    "Higiene": [
        "SABONETE", "SABONET", "SAB LIQ", "SAB LIQUIDO", "SHAMPOO", "SHAMP", "CONDICIONADOR",
        "CONDIC", "CREME DENTAL", "CR DENT", "CR DENTAL", "ESC DENT", "ESC DENTAL", "ESCOVA DENTAL",
        "ESCOVA DENTE", "FIO DENTAL", "FIO DENT", "DESOD", "DESODORANTE", "ANTITRANSP",
        "PAPEL HIG", "PAP HIG", "PAPEL HIGIENICO", "ABSORVENTE", "ABSORV", "FRALDA", "COTONETE",
        "HASTES FLEX", "APARELHO BARBEAR", "APAR BARBEAR", "HIDRATANTE", "PROT SOLAR",
        "PROTETOR SOLAR", "ALGODAO", "ENXAGUANTE BUCAL", "ENXAG", "LENCO UMED", "LENCO UMEDECIDO",
        "CREME CABELO", "CR PENTEAR", "GEL CABELO",
    ],
    "Limpeza": [
        "DETERGENTE", "DET", "LAVA LOUCAS", "LAVA LOUCA", "SABAO", "SAB PO", "SAB EM PO", "SABAO PO",
        "SABAO EM PO", "SAB BARRA", "LAVA ROUPAS", "LAVA ROUPA", "AMACIANTE", "AMAC", "DESINFETANTE",
        "DESINF", "AGUA SANITARIA", "AG SANIT", "AG SANITARIA", "ALVEJANTE", "ALVEJ", "CLORO",
        "MULTIUSO", "MULTI USO", "LIMPA VIDRO", "LUSTRA MOVEIS", "ESPONJA", "PALHA ACO", "PALHA DE ACO",
        "BOMBRIL", "SACO LIXO", "SACO P LIXO", "SACO DE LIXO", "SC LIXO", "VASSOURA", "RODO",
        "PANO", "PANO CHAO", "FLANELA", "INSETICIDA", "LIMPADOR", "SAPONACEO", "TIRA MANCHAS",
        "PAPEL TOALHA", "PAP TOALHA", "LUVA",
    ],
    "Utilidades": [
        "PAPEL ALUMINIO", "PAPEL ALUM", "FILME PVC", "COPO DESC", "COPO DESCARTAVEL", "PRATO DESC",
        "PILHA", "VELA", "FOSFORO", "CARVAO", "GUARDANAPO", "LAMPADA", "ISQUEIRO", "PALITO",
        "SACO FREEZER", "SACO PAO", "FITA", "GAS",
    ],
    "Pet": [
        "RACAO", "AREIA GATO", "AREIA SANIT", "AREIA HIGIENICA", "PETISCO", "SACHE", "PEDIGREE",
        "WHISKAS", "GOLDEN", "PREMIER PET",
    ],
}

PALAVRAS.update({
    "Saúde/Farmácia": [
        "DIPIRONA", "PARACETAMOL", "IBUPROFENO", "AMOXICILINA", "OMEPRAZOL", "LOSARTANA", "BUSCOPAN", "DORFLEX",
        "NEOSALDINA", "TYLENOL", "ASPIRINA", "XAROPE", "VITAMINA", "SUPLEMENTO", "CURATIVO", "TERMOMETRO", "REMEDIO",
        "MEDICAMENTO", "ANTIBIOTICO", "ANALGESICO", "ANTITERMICO", "ANTIALERGICO", "COMPRIMIDOS", "CAPSULAS", "POMADA",
        "PRESERVATIVO", "CAMISINHA",
    ],
    "Casa": [
        "PANELA", "FRIGIDEIRA", "TALHER", "FACA DE COZINHA", "LENCOL", "COBERTOR", "TRAVESSEIRO", "CORTINA", "TAPETE",
        "ORGANIZADOR", "CABIDE", "PRATO RASO", "PRATO FUNDO", "TOALHA DE BANHO", "TOALHA DE MESA", "GARRAFA TERMICA",
        "VENTILADOR", "LIQUIDIFICADOR", "BATEDEIRA",
    ],
    "Combustível": [
        "GASOLINA", "ETANOL", "DIESEL", "GNV", "GASOLINA COMUM", "GASOLINA ADITIVADA", "DIESEL S10", "DIESEL S500",
        "ALCOOL COMBUSTIVEL", "ETANOL HIDRATADO",
    ],
    "Transporte": ["PASSAGEM", "BILHETE", "UBER", "TAXI", "PEDAGIO", "ESTACIONAMENTO", "CORRIDA"],
    "Educação": [
        "MENSALIDADE", "MATRICULA", "CURSO", "APOSTILA", "LIVRO", "CADERNO", "CANETA", "LAPIS", "MOCHILA ESCOLAR",
        "MATERIAL ESCOLAR",
    ],
    "Lazer": ["CINEMA", "INGRESSO", "BRINQUEDO", "BRINQ", "PARQUE", "SHOW", "VIDEO GAME", "JOGO DE TABULEIRO"],
    "Roupas": [
        "CAMISA", "CAMISETA", "CALCA", "BERMUDA", "VESTIDO", "MEIA", "MEIAS", "CUECA", "CALCINHA", "SUTIA", "TENIS",
        "SANDALIA", "CHINELO", "JAQUETA", "BLUSA", "SHORT", "BONE",
    ],
    "Assinaturas": [
        "NETFLIX", "SPOTIFY", "AMAZON PRIME", "YOUTUBE PREMIUM", "DISNEY", "HBO", "ASSINATURA", "GLOBOPLAY", "DEEZER",
        "ICLOUD", "GOOGLE ONE",
    ],
    "Moradia": ["ALUGUEL", "CONDOMINIO", "IPTU", "ENERGIA", "ENERGIA ELETRICA", "AGUA E ESGOTO", "INTERNET", "TAXA DE AGUA"],
    "Carro": [
        "OLEO MOTOR", "OLEO DE MOTOR", "PNEU", "FILTRO DE OLEO", "FILTRO OLEO", "LUBRIFICANTE", "BATERIA",
        "PASTILHA FREIO", "PASTILHA DE FREIO", "ARLA", "LAVAGEM", "ADITIVO", "PALHETA",
    ],
})

CATEGORIAS: list[str] = list(PALAVRAS.keys()) + [OUTROS]

# Categoria usada quando só o tipo do estabelecimento ajuda (nenhuma palavra-chave casou).
DEFAULT_POR_TIPO = {
    "farmacia": "Saúde/Farmácia", "posto": "Combustível", "casa_utilidades": "Casa", "vestuario": "Roupas",
    "educacao": "Educação", "transporte": "Transporte", "lazer": "Lazer", "padaria": "Padaria",
}


def _montar():
    frases: list[tuple[re.Pattern, str]] = []
    palavras: dict[str, str] = {}
    for cat, termos in PALAVRAS.items():
        for termo in termos:
            t = normalizar_nome(termo)
            if " " in t:
                frases.append((t, cat))
            else:
                palavras.setdefault(t, cat)
    frases.sort(key=lambda fc: len(fc[0]), reverse=True)
    frases_re = [(re.compile(r"\b" + re.escape(f) + r"\b"), c) for f, c in frases]
    # Alternativas mais longas primeiro: na mesma posição, a regex fica com a maior.
    alternativas = sorted(palavras, key=len, reverse=True)
    palavras_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in alternativas) + r")\b")
    return frases_re, palavras_re, palavras


_FRASES, _PALAVRAS_RE, _PALAVRA_CAT = _montar()


def categorizar(nome: str) -> str:
    texto = normalizar_nome(nome or "")
    if not texto:
        return OUTROS
    for padrao, cat in _FRASES:
        if padrao.search(texto):
            return cat
    m = _PALAVRAS_RE.search(texto)
    if m:
        return _PALAVRA_CAT[m.group(1)]
    return OUTROS
