# Release process for Permifrost 


## When to release

* Usually, will kick off the release `2` times per year (each `6` months). Do not want to be strict, probably `September` and `March` are good options. The person who is maintainer for the week will proceed with the new version release process
* 
### Pre-requirements

Check list:
* [ ] Create an issue with [Releasing update.md](https://gitlab.com/gitlab-data/permifrost/-/blob/master/.gitlab/issue_templates/Releasing%20update.md) template and following the check list.


## Who to release

* Any `@gitlab-data/permifrost_maintainers` is authorised to do deployment

## Version naming

Using the [Semantic Versioning 2.0.](https://semver.org/)


## How to release
* If all approvals are added, merge the MR, as per checklist from the [template](https://gitlab.com/gitlab-data/permifrost/-/blob/master/.gitlab/issue_templates/Releasing%20update.md).

### Release diagram

``` mermaid
flowchart TD
    Start((Start)) --> RELEASE_TEMPLATE
    END((End))
    Maintainers --> CLOSE
    RELEASE_TEMPLATE --> ISSUE
    CLOSE --> PYPI
    PYPI --> END
    
    subgraph Approvals
        ISSUE[Issue created] --> Maintainers[maintainers x3] 
        CLOSE[Closing the issue/Merge MR]
    end
    
    subgraph Prerequirements
        RELEASE_TEMPLATE 
    end
    
    subgraph Deployment
        PYPI[PyPi publishing] 
    end
```