import pytest

from smartmercado.categorias import CATEGORIAS, OUTROS, categorizar


@pytest.mark.parametrize("nome,cat", [
    # abreviações típicas de NFC-e
    ("REFRIG COCA COLA 2L", "Bebidas"),
    ("REFRIG. GUARANA ANTARCTICA 350ML", "Bebidas"),
    ("DESOD REXONA AERO 150ML", "Higiene"),
    ("DET YPE NEUTRO 500ML", "Limpeza"),
    ("PAPEL HIG NEVE FD 12X30M", "Higiene"),
    ("PAP HIG PERSONAL 4UN", "Higiene"),
    ("CERV HEINEKEN LN 330ML", "Bebidas Alcoólicas"),
    ("CR DENT COLGATE 90G", "Higiene"),
    ("QJO MUSS FATIADO KG", "Frios e Laticínios"),
    ("LT COND MOCOCA 395G", "Mercearia"),
    ("AMAC DOWNY 500ML", "Limpeza"),
    ("BISC RECHEADO OREO", "Mercearia"),
    # frases mais longas vencem palavras isoladas
    ("AGUA SANITARIA QBOA 1L", "Limpeza"),
    ("AGUA MINERAL CRYSTAL 500ML", "Bebidas"),
    ("CREME DE LEITE NESTLE 200G", "Mercearia"),
    ("LEITE INTEGRAL ITALAC 1L", "Frios e Laticínios"),
    ("PAPEL TOALHA SNOB", "Limpeza"),
    ("BATATA PALHA YOKI", "Mercearia"),
    ("BATATA DOCE KG", "Hortifruti"),
    ("SABAO EM PO OMO 1KG", "Limpeza"),
    ("PAO DE QUEIJO FORNO DE MINAS", "Padaria"),
    # sem frase: vale a primeira palavra-chave do nome
    ("SUCO DEL VALLE LARANJA 1L", "Bebidas"),
    ("MOLHO TOMATE POMAROLA", "Mercearia"),
    ("LARANJA PERA KG", "Hortifruti"),
    # acentos/pontuação/caixa não importam
    ("Feijão Carioca 1kg", "Mercearia"),
    ("açúcar cristal", "Mercearia"),
    ("Linguiça Toscana Sadia", "Carnes"),
])
def test_categorias(nome, cat):
    assert categorizar(nome) == cat


@pytest.mark.parametrize("nome", [
    "DETALHE DECORATIVO",      # DET só como palavra inteira
    "SALSICHAO",               # SAL / SALSICHA não casam dentro de outra palavra
    "CERVICAL ORTOPEDICO",     # CERV não casa em CERVICAL
    "REFRIGERADOR PORTATIL",   # REFRIG não casa em REFRIGERADOR
    "",
])
def test_palavras_inteiras(nome):
    assert categorizar(nome) == OUTROS


def test_categorias_validas():
    assert OUTROS in CATEGORIAS
    assert len(set(CATEGORIAS)) == len(CATEGORIAS)
