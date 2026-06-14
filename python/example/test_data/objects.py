from __future__ import annotations

import dataclasses
from enum import StrEnum

import pydantic


class Species(StrEnum):
    DOG = "Dog"
    CAT = "Cat"
    MONKEY = "Monkey"
    PIG = "Pig"


@dataclasses.dataclass
class PetDataclass:
    name: str
    age: float
    species: Species = Species.MONKEY


@dataclasses.dataclass
class PersonDataclass:
    first_name: str
    last_name: str
    id: str
    optional: str | None
    complex: dict[str, list[dict[str, str]]]
    pets: list[PetDataclass]


class PetPydantic(pydantic.BaseModel):
    name: str
    age: float
    species: Species = Species.MONKEY


class PersonPydantic(pydantic.BaseModel):
    first_name: str
    last_name: str
    id: str
    optional: str | None
    complex: dict[str, list[dict[str, str]]]
    pets: list[PetPydantic] = pydantic.Field(len=1)


def load(raw_data: dict) -> tuple[list[PersonPydantic], list[PersonDataclass]]:
    pydantic_data = pydantic.parse_obj_as(list[PersonPydantic], raw_data)
    dataclass_data = []
    for person_data in raw_data:
        kwargs = {**person_data}
        if person_data["pets"]:
            kwargs["pets"] = [PetDataclass(**pet) for pet in person_data["pets"]]
        dataclass_data.append(PersonDataclass(**kwargs))
    return pydantic_data, dataclass_data
