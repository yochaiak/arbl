"""Allowlisted, stable list ordering and literal substring search."""

from fastapi import HTTPException


def text_match(column, query):
    return column.icontains(query.strip(), autoescape=True)


def ordered(query, order, columns):
    if order not in columns:
        raise HTTPException(422, "Unsupported order; choose " + ", ".join(columns))
    return query.order_by(None).order_by(*columns[order])


def name_orders(model):
    return {
        "newest": (model.created_at.desc(), model.id.desc()),
        "oldest": (model.created_at.asc(), model.id.asc()),
        "name_asc": (model.name.asc(), model.id.asc()),
        "name_desc": (model.name.desc(), model.id.desc()),
    }
