"""HTTP surfaces. One module per audience.

    buyer.py     someone buying one piece
    b2b.py       someone buying five hundred
    passport.py  someone holding the object, scanning the tag
    api.py       the same data as JSON, for A1's app and anyone after C1

They are separate files because they are separate audiences with
separate failure modes, not because the URLs happen to differ. The B2B
portal has to be able to say "no, not by that date"; the storefront has
to be able to say "not for sale until we have asked the artisan"; the
passport page has to work on a phone with one bar of signal at a mela.
"""
